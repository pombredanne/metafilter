#!/usr/bin/python
from metafilter.model import Session
import metafilter.model
from metafilter.model.nodes import update_nodes_from_path, remove_orphans, calc_md5
from datetime import datetime
import sys
from optparse import OptionParser

import logging
import logging.handlers

LOG = logging.getLogger(__name__)
LOG.setLevel(logging.DEBUG)

ERROR_HANDLER = logging.handlers.RotatingFileHandler(filename='errors.log', maxBytes=102400, backupCount=5)
CONSOLE_HANDLER = logging.StreamHandler()
ERROR_HANDLER.setLevel(logging.WARNING)
CONSOLE_HANDLER.setLevel(logging.WARNING)
FORMATTER = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
ERROR_HANDLER.setFormatter(FORMATTER)
CONSOLE_HANDLER.setFormatter(FORMATTER)
logging.getLogger().setLevel(logging.DEBUG)
logging.getLogger().addHandler(ERROR_HANDLER)
logging.getLogger().addHandler(CONSOLE_HANDLER)

def main():
    parser = OptionParser()
    parser.add_option("-d", "--dsn", dest="dsn",
                            help="Database DSN (see sqlalchemy docs for details)",
                            metavar="DSN")
    parser.add_option("-s", "--since", dest="since", default=None,
                            help="Only scan file that changed after this date (format: YYYY-MM-DD)")
    parser.add_option("-n", "--no-insert", dest="no_insert", default=False, action='store_true',
                            help="Do not insert or update new nodes. This may be usefule if you only want to remove old entries, or calculate the md5 sums")
    parser.add_option("-p", "--purge", dest="purge", default=False, action="store_true",
                help="Remove orphans after scan. WARNING: this removes file not available on disk! If you work with removable devices, the device should be mounted and available before running this. Otherwise all files on from that device will be removed from the index!")
    parser.add_option("-m", "--md5", dest="md5", default=False, action="store_true",
                help="Calculate md5sums after scan. This can take a long time, but is necessary to detect moves & duplictates")
    parser.add_option("-a", "--auto-tag-tail", dest="auto_tag_tail", default=False, action="store_true",
                help="Automatically add the 'leaf' folder-name to the tags")
    parser.add_option("-w", "--auto-tag-word", dest="auto_tag_words", default=[], action="append", metavar="WORD",
                help="If WORD appears anywhere as folder-name in the files path, add it to the tag list. This option can be specified as many times you want.")
    parser.add_option("-v", "--verbose", dest="verbose", default=False, action="store_true",
                help="Verbose output to stdout")

    (options, args) = parser.parse_args()

    if options.verbose:
        CONSOLE_HANDLER.setLevel(logging.DEBUG)

    if not options.dsn:
        print >> sys.stderr, "The '-d/--dsn' option is required!"
        sys.exit(9)

    metafilter.model.set_dsn(options.dsn)

    if options.since:
        try:
            options.since = datetime.strptime(options.since, "%Y-%m-%d")
        except Exception, exc:
            LOG.error(exc)
            options.since = None


    sess = Session()

    if not options.no_insert:
        update_nodes_from_path(sess, args[0], options.since,
                options.auto_tag_tail, options.auto_tag_words)

    if options.purge:
        remove_orphans(sess, args[0])

    if options.md5:
        calc_md5(sess, args[0], options.since)

    sess.close()
    print "Rescan finished"

if __name__ == '__main__':
    main()

