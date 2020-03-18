#!/usr/bin/python3

from reviewer import Reviewer
from gerrit import Gerrit, GerritRevision, GerritMessage

from trollconfig import TrollConfig
from trollreview import ReviewType
from trollreviewerfromgit import FromgitChangeReviewer
from trollreviewerupstream import UpstreamChangeReviewer
from trollreviewerfromlist import FromlistChangeReviewer
from trollreviewerchromium import ChromiumChangeReviewer
from trollstats import TrollStats

import argparse
import datetime
import json
import logging
from logging import handlers
import requests
import sys
import time

logger = logging.getLogger('rom')
logger.setLevel(logging.DEBUG) # leave this to handlers

class Troll(object):
  def __init__(self, config):
    self.config = config
    self.gerrit = Gerrit(config.gerrit_url)
    self.tag = 'autogenerated:review-o-matic'
    self.blacklist = {}
    self.stats = TrollStats('{}'.format(self.config.stats_file))

  def do_review(self, project, change, review):
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

    self.stats.update_for_review(project, review)

    self.gerrit.review(change, self.tag, review.generate_review_message(),
                       review.notify, vote_code_review=review.vote,
                       inline_comments=review.inline_comments)

    if self.config.results_file:
      with open(self.config.results_file, 'a+') as f:
        f.write('{}: Issues: {}, Feedback: {}, Vote:{}, Notify:{}\n'.format(
          change.url(), review.issues.keys(), review.feedback.keys(),
          review.vote, review.notify))

  def get_changes(self, project, prefix):
    message = '{}:'.format(prefix)
    after = datetime.date.today() - datetime.timedelta(days=5)
    changes = self.gerrit.query_changes(status='open', message=message,
                    after=after, project=project.gerrit_project)
    return changes

  def add_change_to_blacklist(self, change):
    self.blacklist[change.number] = change.current_revision.number

  def is_change_in_blacklist(self, change):
    return self.blacklist.get(change.number) == change.current_revision.number

  def process_change(self, project, rev, c):
    logger.debug('Processing change {}'.format(c.url()))

    force_review = self.config.force_cl or self.config.force_all

    age_days = None
    if not force_review:
      for m in c.messages:
        if m.tag == self.tag and m.revision_num == c.current_revision.number:
          age_days = (datetime.datetime.utcnow() - m.date).days

    if age_days != None:
      logger.debug('    Reviewed {} days ago'.format(age_days))

    # Find a reviewer and blacklist if not found
    reviewer = None
    if FromlistChangeReviewer.can_review_change(project, c, age_days):
      reviewer = FromlistChangeReviewer(project, rev, c, self.config.dry_run)
    elif FromgitChangeReviewer.can_review_change(project, c, age_days):
      reviewer = FromgitChangeReviewer(project, rev, c, self.config.dry_run,
                                       age_days)
    elif UpstreamChangeReviewer.can_review_change(project, c, age_days):
      reviewer = UpstreamChangeReviewer(project, rev, c, self.config.dry_run)
    elif ChromiumChangeReviewer.can_review_change(project, c, age_days):
      reviewer = ChromiumChangeReviewer(project, rev, c, self.config.dry_run,
                                        self.config.verbose)
    if not reviewer:
      self.add_change_to_blacklist(c)
      return None

    if not force_review and self.is_change_in_blacklist(c):
      return None

    return reviewer.review_patch()

  def process_changes(self, project, changes):
    rev = Reviewer(git_dir=project.local_repo, verbose=self.config.verbose,
                   chatty=self.config.chatty)
    ret = 0
    for c in changes:
      try:
        result = self.process_change(project, rev, c)
        if result:
          self.do_review(project, c, result)
          ret += 1
      except Exception as e:
        logger.error('Exception processing change {}'.format(c.url()))
        logger.exception('Exception: {}'.format(e))

      self.add_change_to_blacklist(c)

    return ret

  def run(self):
    if self.config.force_cl:
      c = self.gerrit.get_change(self.config.force_cl, self.config.force_rev)
      logger.info('Force reviewing change  {}'.format(c))
      project = self.config.get_project(c.project)
      if not project:
        raise ValueError('Could not find project!')
      self.process_changes(project, [c])
      self.stats.summarize(logging.INFO)
      return

    while True:
      try:
        did_review = 0
        for project in self.config.projects.values():
          if (self.config.force_project and
              project.name != self.config.force_project):
            continue
          logger.debug('Running for project {}'.format(project.name))
          for p in project.prefixes:
            changes = self.get_changes(project, p)
            logger.debug('{} changes for prefix {}'.format(len(changes), p))
            did_review += self.process_changes(project, changes)
          if did_review > 0:
            self.stats.summarize(logging.INFO)
            if not self.config.dry_run:
              self.stats.save()

        if not self.config.daemon:
          return
        logger.debug('Finished! Going to sleep until next run')

      except (requests.exceptions.HTTPError, OSError) as e:
        logger.error('Error getting changes: ({})'.format(str(e)))
        logger.exception('Exception getting changes: {}'.format(e))
        time.sleep(60)

      time.sleep(120)


def setup_logging(config):
  info_handler = logging.StreamHandler(sys.stdout)
  info_handler.setFormatter(logging.Formatter('%(levelname)6s - %(name)s - %(message)s'))
  if config.verbose:
    info_handler.setLevel(logging.DEBUG)
  else:
    info_handler.setLevel(logging.INFO)
  logger.addHandler(info_handler)

  if not config.log_file or config.dry_run:
    return

  err_handler = logging.handlers.RotatingFileHandler(config.log_file,
                                                     maxBytes=10000000,
                                                     backupCount=20)
  err_handler.setLevel(logging.WARNING)
  f = logging.Formatter(
        '%(asctime)s %(levelname)8s - %(name)s.%(funcName)s:%(lineno)d - ' +
        '%(message)s')
  err_handler.setFormatter(f)
  logger.addHandler(err_handler)


def main():
  config = TrollConfig()

  setup_logging(config)

  troll = Troll(config)
  troll.run()

if __name__ == '__main__':
  sys.exit(main())
