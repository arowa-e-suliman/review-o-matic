#!/usr/bin/python3

from reviewer import Reviewer
from gerrit import Gerrit, GerritRevision, GerritMessage

from trollreview import ReviewType
from trollreviewerfromgit import FromgitChangeReviewer
from trollreviewerupstream import UpstreamChangeReviewer
from trollreviewerfromlist import FromlistChangeReviewer
from trollreviewerchromium import ChromiumChangeReviewer

import argparse
import datetime
import json
import logging
from logging import handlers
import requests
import sys
import time

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG) # leave this to handlers

class Troll(object):
  def __init__(self, url, args):
    self.url = url
    self.args = args
    self.gerrit = Gerrit(url)
    self.tag = 'autogenerated:review-o-matic'
    self.blacklist = {}
    self.stats = {
        str(ReviewType.SUCCESS): 0, str(ReviewType.BACKPORT): 0,
        str(ReviewType.ALTERED_UPSTREAM): 0,
        str(ReviewType.MISSING_FIELDS): 0,
        str(ReviewType.MISSING_HASH): 0,
        str(ReviewType.INVALID_HASH): 0,
        str(ReviewType.MISSING_AM): 0,
        str(ReviewType.INCORRECT_PREFIX): 0,
        str(ReviewType.FIXES_REF): 0,
        str(ReviewType.KCONFIG_CHANGE): 0,
        str(ReviewType.IN_MAINLINE): 0,
        str(ReviewType.UPSTREAM_COMMENTS): 0,
        str(ReviewType.FORBIDDEN_TREE): 0
    }

  def inc_stat(self, review_type):
    if self.args.dry_run:
      return
    key = str(review_type)
    if not self.stats.get(key):
      self.stats[key] = 1
    else:
      self.stats[key] += 1

  def do_review(self, change, review):
    logger.info('Review for change: {}'.format(change.url()))
    logger.info('  Issues: {}, Feedback: {}, Vote:{}, Notify:{}'.format(
        review.issues.keys(), review.feedback.keys(), review.vote,
        review.notify))

    if review.dry_run:
      print(review.generate_review_message())
      if review.inline_comments:
        print('')
        print('-- Inline comments:')
        for f,comments in review.inline_comments.items():
          for c in comments:
            print('{}:{}'.format(f, c['line']))
            print(c['message'])

      print('------')
      return

    for i in review.issues:
      self.inc_stat(i)
    for f in review.feedback:
      self.inc_stat(f)
    self.gerrit.review(change, self.tag, review.generate_review_message(),
                       review.notify, vote_code_review=review.vote,
                       inline_comments=review.inline_comments)

  def get_changes(self, prefix):
    message = '{}:'.format(prefix)
    after = datetime.date.today() - datetime.timedelta(days=5)
    changes = self.gerrit.query_changes(status='open', message=message,
                    after=after, project='chromiumos/third_party/kernel')
    return changes

  def add_change_to_blacklist(self, change):
    self.blacklist[change.number] = change.current_revision.number

  def is_change_in_blacklist(self, change):
    return self.blacklist.get(change.number) == change.current_revision.number

  def process_change(self, rev, c):
    logger.debug('Processing change {}'.format(c.url()))

    force_review = self.args.force_cl or self.args.force_all

    days_since_last_review = None
    if not force_review:
      for m in c.messages:
        if m.tag == self.tag and m.revision_num == c.current_revision.number:
          days_since_last_review = (datetime.datetime.utcnow() - m.date).days

    if  days_since_last_review != None:
      logger.debug('    Reviewed {} days ago'.format(days_since_last_review))

    # Find a reviewer and blacklist if not found
    reviewer = None
    if FromlistChangeReviewer.can_review_change(c, days_since_last_review):
      reviewer = FromlistChangeReviewer(rev, c, self.args.dry_run)
    elif FromgitChangeReviewer.can_review_change(c, days_since_last_review):
      reviewer = FromgitChangeReviewer(rev, c, self.args.dry_run,
                                       days_since_last_review)
    elif UpstreamChangeReviewer.can_review_change(c, days_since_last_review):
      reviewer = UpstreamChangeReviewer(rev, c, self.args.dry_run)
    elif self.args.kconfig_hound and \
        ChromiumChangeReviewer.can_review_change(c, days_since_last_review):
      reviewer = ChromiumChangeReviewer(rev, c, self.args.dry_run,
                                        self.args.verbose)
    if not reviewer:
      self.add_change_to_blacklist(c)
      return None

    if not force_review and self.is_change_in_blacklist(c):
      return None

    return reviewer.review_patch()

  def process_changes(self, changes):
    rev = Reviewer(git_dir=self.args.git_dir, verbose=self.args.verbose,
                   chatty=self.args.chatty)
    ret = 0
    for c in changes:
      try:
        result = self.process_change(rev, c)
        if result:
          self.do_review(c, result)
          ret += 1
      except Exception as e:
        logger.error('Exception processing change {}'.format(c.url()))
        logger.exception('Exception: {}'.format(e))

      self.add_change_to_blacklist(c)

    return ret

  def update_stats(self):
    if not self.args.dry_run and self.args.stats_file:
      with open(self.args.stats_file, 'wt') as f:
        json.dump(self.stats, f)
    summary = '  Summary: '
    total = 0
    for k,v in self.stats.items():
      summary += '{}={} '.format(k,v)
      total += v
    summary += 'total={}'.format(total)
    logger.info(summary)

  def run(self):
    if self.args.force_cl:
      c = self.gerrit.get_change(self.args.force_cl, self.args.force_rev)
      logger.info('Force reviewing change  {}'.format(c))
      self.process_changes([c])
      return

    if self.args.stats_file:
      try:
        with open(self.args.stats_file, 'rt') as f:
          self.stats = json.load(f)
      except FileNotFoundError:
        self.update_stats()

    prefixes = ['UPSTREAM', 'BACKPORT', 'FROMGIT', 'FROMLIST']
    if self.args.kconfig_hound:
      prefixes += ['CHROMIUM']

    if self.args.force_prefix:
      prefixes = [self.args.force_prefix]

    while True:
      try:
        did_review = 0
        for p in prefixes:
          changes = self.get_changes(p)
          logger.debug('{} changes for prefix {}'.format(len(changes), p))
          did_review += self.process_changes(changes)
        if did_review > 0:
          self.update_stats()
        if not self.args.daemon:
          break
        logger.debug('Finished! Going to sleep until next run')

      except (requests.exceptions.HTTPError, OSError) as e:
        logger.error('Error getting changes: ({})'.format(str(e)))
        logger.exception('Exception getting changes: {}'.format(e))
        time.sleep(60)

      time.sleep(120)


def setup_logging(args):
  info_handler = logging.StreamHandler(sys.stdout)
  info_handler.setFormatter(logging.Formatter('%(levelname)8s - %(message)s'))
  if args.verbose:
    info_handler.setLevel(logging.DEBUG)
  else:
    info_handler.setLevel(logging.INFO)
  logger.addHandler(info_handler)

  if not args.err_logfile:
    return

  err_handler = logging.handlers.RotatingFileHandler(args.err_logfile,
                                                     maxBytes=10000000,
                                                     backupCount=20)
  err_handler.setLevel(logging.WARNING)
  f = logging.Formatter(
        '%(asctime)s %(levelname)8s - %(name)s.%(funcName)s:%(lineno)d - ' +
        '%(message)s')
  err_handler.setFormatter(f)
  logger.addHandler(err_handler)


def main():
  parser = argparse.ArgumentParser(description='Troll gerrit reviews')
  parser.add_argument('--git-dir', default=None, help='Path to git directory')
  parser.add_argument('--verbose', help='print commits', action='store_true')
  parser.add_argument('--chatty', help='print diffs', action='store_true')
  parser.add_argument('--daemon', action='store_true',
    help='Run in daemon mode, for continuous trolling')
  parser.add_argument('--dry-run', action='store_true', default=False,
                      help='skip the review step')
  parser.add_argument('--force-cl', default=None, help='Force review a CL')
  parser.add_argument('--force-rev', default=None,
                      help=('Specify a specific revision of the force-cl to '
                           'review (ignored if force-cl is not true)'))
  parser.add_argument('--force-all', action='store_true', default=False,
                      help='Force review all (implies dry-run)')
  parser.add_argument('--force-prefix', default=None,
                      help='Only search for the provided prefix')
  parser.add_argument('--stats-file', default=None, help='Path to stats file')
  parser.add_argument('--kconfig-hound', default=None, action='store_true',
    help='Compute and post the total difference for kconfig changes')
  parser.add_argument('--err-logfile', default=None, help='Path to ERROR log')
  args = parser.parse_args()

  setup_logging(args)

  if args.force_all:
    args.dry_run = True

  troll = Troll('https://chromium-review.googlesource.com', args)
  troll.run()

if __name__ == '__main__':
  sys.exit(main())
