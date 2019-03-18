import requests
  # Whitelisted patchwork hosts
  PATCHWORK_WHITELIST = [
    'lore.kernel.org',
    'patchwork.freedesktop.org',
    'patchwork.kernel.org',
    'patchwork.linuxtv.org',
    'patchwork.ozlabs.org'
  ]

    cmd = self.git_cmd + ['log', '--format=oneline', '--abbrev-commit', '-i',
                          '--grep', 'Fixes:.*{}'.format(sha[:8]),
  def get_am_from_from_patch(self, patch):
    regex = re.compile('\(am from (http.*)\)', flags=re.I)
    m = regex.findall(patch)
    if not m or not len(m):
      return None
    return m

  def get_commit_from_patchwork(self, url):
    regex = re.compile('https://([a-z\.]*)/([a-z/]*)/([0-9]*)/')
    m = regex.match(url)
    if not m or not (m.group(1) in self.PATCHWORK_WHITELIST):
      sys.stderr.write('ERROR: URL "%s"\n' % url)
      return None
    return requests.get(url + 'raw/').text
