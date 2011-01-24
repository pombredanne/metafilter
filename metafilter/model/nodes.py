from sqlalchemy import Table, Column, Integer, Unicode, ForeignKey, String, DateTime, Boolean, UniqueConstraint, Sequence, select, func, update, not_
from sqlalchemy.orm import mapper, aliased, relation
from sqlalchemy.sql import func, distinct
from sqlalchemy.exc import IntegrityError, DataError
from metafilter.model import metadata, uri_to_ltree, file_md5, uri_depth
from metafilter.model.queries import Query, query_table
from os.path import sep, isdir, basename, exists
from datetime import datetime, timedelta
import re
from sys import getfilesystemencoding

import parsedatetime.parsedatetime as pdt
import parsedatetime.parsedatetime_consts as pdc

import logging

from metafilter.model import memoized

nodes_table = Table('node', metadata,
   Column('uri', Unicode, nullable=False, primary_key=True),
   Column('path', String),
   Column('md5', String(32)),
   Column('mimetype', String(32)),
   Column('created', DateTime),
   Column('updated', DateTime),
   Column('to_purge', Boolean, default=False),
   Column('rating', Integer, default=0),
   UniqueConstraint('uri', name='unique_uri')
)

tag_table = Table('tag', metadata,
   Column('name', Unicode, nullable=False, primary_key=True),
)

node_has_tag_table = Table('node_has_tag', metadata,
   Column('uri', Unicode, ForeignKey('node.uri'), nullable=False, primary_key=True),
   Column('tag', Unicode, ForeignKey('tag.name'), nullable=False, primary_key=True),
)

acknowledged_duplicates_table = Table('acknowledged_duplicates', metadata,
   Column('md5', String, nullable=False, primary_key=True),
)

TIME_PATTERN=re.compile(r'(\d{4}-\d{2}-\d{2})?(t)?(\d{4}-\d{2}-\d{2})?')
LOG = logging.getLogger(__name__)
PCONST = pdc.Constants()
CALENDAR = pdt.Calendar(PCONST)
FLATTEN_MAP = {}

def by_uri(session, uri):
   qry = session.query(Node)
   qry = qry.filter( Node.uri == uri )
   return qry.first()

def by_path(session, path):
   qry = session.query(Node)
   qry = qry.filter( Node.path == path )
   return qry.first()

def update_nodes_from_path(sess, root, oldest_refresh=None):
   import os
   import mimetypes
   mimetypes.init()
   from os.path import isfile, join, abspath, sep

   root_ltree = uri_to_ltree(root)
   if not oldest_refresh:
      oldest_refresh = select([func.max(Node.updated)])
      oldest_refresh = oldest_refresh.where( Node.path.op("<@")(root_ltree) )
      oldest_refresh = oldest_refresh.execute().first()[0]

   LOG.info("Rescanning files that changed since %s" % oldest_refresh)

   for root, dirs, files in os.walk(root):

      # store folder nodes
      for node in root.split(sep):
         try:
            detached_file = Node(root.decode(getfilesystemencoding()))
         except UnicodeDecodeError, exc:
            LOG.error("%r: %s" % (path, exc))
            continue

         detached_file.mimetype = "other/directory"

         try:
            LOG.debug("Merging %s" % detached_file)
            attached_file = sess.merge(detached_file)
            sess.add(attached_file)
            LOG.debug("Added %s" % attached_file)
            sess.commit()
         except IntegrityError, exc:
            if exc.message.strip() == '(IntegrityError) duplicate key value violates unique constraint "node_path"':
               LOG.warning(exc.message)
               LOG.warning(exc.params)
               sess.rollback()
            else:
               raise
         except DataError, exc:
            if "(DataError) invalid byte sequence for encoding" in exc.message:
               LOG.warning(exc.message)
               LOG.warning(exc.params)
               sess.rollback()
            else:
               raise

      if 'Thumbs.db' in files:
         files.remove('Thumbs.db')

      scanned_files = 0
      for file in files:
         path = abspath(join(root, file))
         if not isfile(path):
            LOG.warning("Not a regular file: %r" % path)
            continue

         mod_time = max(
               datetime.fromtimestamp(os.stat(path).st_mtime),
               datetime.fromtimestamp(os.stat(path).st_ctime)
               )
         create_time = datetime.fromtimestamp(os.stat(path).st_ctime)

         # ignore files which have not been modified since last scan
         if oldest_refresh and mod_time < oldest_refresh:
            continue

         mimetype, _ = mimetypes.guess_type(path)

         try:
            detached_file = Node(path.decode(getfilesystemencoding()))
            detached_file.mimetype = mimetype
            detached_file.created = create_time
            detached_file.updated = mod_time

            attached_file = sess.merge(detached_file)
            sess.add(attached_file)
            sess.commit()
            LOG.info("Added %s" % attached_file)
         except Exception, exc:
            LOG.error("%r: %s" % (path, exc))
            sess.rollback()
         scanned_files += 1

      if scanned_files > 0:
         LOG.info("commit")
         sess.commit()

      if 'CVS' in dirs:
         dirs.remove('CVS')  # don't visit CVS directories

      if '.git' in dirs:
         dirs.remove('.git')  # don't visit CVS directories

      if '.svn' in dirs:
         dirs.remove('.svn')  # don't visit CVS directories

   sess.commit()

def set_tags(sess, uri, new_tags):
   node = by_uri(sess, uri)
   if not node:
      return

   for tag in node.tags:
      if tag.name not in new_tags:
         node.tags.remove(tag)

   for tag in new_tags:
      if tag not in node.tags:
         tmp = Tag.find(sess, tag)
         if not tmp:
            tmp = Tag(tag)
         node.tags.append(tmp)

   sess.merge(node)
   sess.flush()

def remove_empty_dirs(sess, root):
   root_ltree = uri_to_ltree(root)
   nodes = root_ltree.split('.')

   if not nodes:
      return

   qry = select([Node.path])
   qry = qry.where( Node.path.op("<@")('.'.join(nodes)) )
   qry = qry.where( Node.mimetype == 'other/directory' )
   child_nodes = [ row[0] for row in qry.execute() ]

   for node in child_nodes:
      qry = select([func.count(Node.uri)])
      qry = qry.where( Node.path.op("<@")(node) )
      for row in qry.execute():
         if row[0] == 1:
            LOG.debug('Removing empty dir: %r' % node)
            nodes_table.delete(nodes_table.c.path == node).execute()

def remove_orphans(sess, root):
   root_ltree = uri_to_ltree(root)
   qry = select([Node.uri, Node.mimetype])
   qry = qry.where( Node.path.op("<@")(root_ltree) )
   for row in qry.execute():
      if not exists(row[0]):
         LOG.info('Removing orphan %r' % row[0])
         try:
            nodes_table.delete(nodes_table.c.uri == row[0]).execute()
            sess.commit()
         except:
            sess.rollback()

   remove_empty_dirs(sess, root)

def calc_md5(sess, root):
   root_ltree = uri_to_ltree(root)
   qry = sess.query(Node)
   qry = qry.filter( Node.path.op("<@")(root_ltree) )
   qry = qry.filter( Node.mimetype != 'other/directory' )
   count = 0
   for node in qry:
      if not exists(node.uri):
         continue
      node.md5 = file_md5(node.uri)
      LOG.info('Updated md5 of %s' % node)
      count += 1

      if count % 500 == 0:
         # commit from time to time
         sess.commit()
         LOG.info('commit')
   sess.commit()
   LOG.info('commit')

def rated(stmt, parent_uri, nodes):

   op = nodes.pop(0)
   value = int(nodes.pop(0))

   LOG.debug("Finding entries rated %s %2d in %s" % (op, value, parent_uri))

   if op == 'gt':
      stmt = stmt.filter(Node.rating > value)
   elif op == 'ge':
      stmt = stmt.filter(Node.rating >= value)
   elif op == 'lt':
      stmt = stmt.filter(Node.rating < value)
   elif op == 'le':
      stmt = stmt.filter(Node.rating <= value)
   elif op == 'eq':
      stmt = stmt.filter(Node.rating == value)
   elif op == 'ne':
      stmt = stmt.filter(Node.rating != value)

   return stmt

def in_path(stmt, nodes):

   substring = nodes.pop(0)
   LOG.debug("Finding entries containing %s in path" % (substring))
   stmt = stmt.filter(Node.uri.ilike('%%%s%%' % substring))
   return stmt

def has_md5(stmt, parent_uri, nodes):

   md5 = nodes.pop(0)
   LOG.debug("Finding entries with md5 %s" % (md5))
   stmt = stmt.filter(Node.md5 == md5)
   return stmt

def all(sess, nodes, flatten=False):

   parent_uri = '/'.join(nodes)

   parent_path = uri_to_ltree(parent_uri)
   depth = uri_depth(parent_uri)

   stmt = sess.query(
         distinct(func.subpath(Node.path, 0, depth+1).label("subpath"))
         )

   stmt = stmt.filter( Node.path.op("<@")(parent_path) )
   stmt = stmt.subquery()
   qry = sess.query( Node )
   qry = qry.filter( Node.path.in_(stmt) )

   return qry

def dated(sess, stmt, parent_uri, nodes):

   date_string = nodes.pop(0)

   LOG.debug("Finding entries using date string %s in %r" % (date_string, parent_uri))

   match = TIME_PATTERN.match(date_string)
   if  match and match.groups() != (None, None, None):
      groups = match.groups()
      if groups[0] and not groups[1] and not groups[2]:
         # matches 'yyyy-mm-dd'
         end_date = datetime.strptime(groups[0], "%Y-%m-%d")
         stmt = stmt.filter(Node.created < end_date)
      elif groups[0] and groups[1] == "t" and not groups[2]:
         # matches 'yyyy-mm-ddt'
         start_date = datetime.strptime(groups[0], "%Y-%m-%d")
         stmt = stmt.filter(Node.created > start_date)
      elif not groups[0] and groups[1] == "t" and groups[2]:
         # matches 'tyyyy-mm-dd'
         end_date = datetime.strptime(groups[2], "%Y-%m-%d")
         stmt = stmt.filter(Node.created < end_date)
      elif groups[0] and groups[1] == "t" and groups[2]:
         # matches 'yyyy-mm-ddtyyyy-mm-dd'
         start_date = datetime.strptime(groups[0], "%Y-%m-%d")
         end_date = datetime.strptime(groups[2], "%Y-%m-%d")
         stmt = stmt.filter(Node.created.between(start_date, end_date))

   timetuple = CALENDAR.parse(date_string)
   start_date = datetime(*timetuple[0][0:6])
   stmt = stmt.filter(Node.created > start_date)
   return stmt

def tagged(sess, stmt, parent_uri, nodes):

   query_string = 'tag/%s' % str.join('/', nodes)

   tag_string = nodes.pop(0)

   LOG.debug("Finding entries using tag string %s in %r" % (tag_string, parent_uri))

   tags = tag_string.split(',')

   for tag_name in tags:
      tag = Tag.find(sess, tag_name)
      if not tag:
         continue
      stmt = stmt.filter(Node.tags.contains(tag))

   return stmt

def duplicates(sess):

   acks = select([acknowledged_duplicates_table.c.md5])

   qry = sess.query(Node.md5, func.count(Node.md5))
   qry = qry.filter(not_(Node.md5.in_(acks)))
   qry = qry.group_by(Node.md5)
   qry = qry.having(func.count(Node.md5) > 1)
   qry = qry.order_by(func.count(Node.md5).desc())
   return qry

def acknowledge_duplicate(sess, md5):
   acknowledged_duplicates_table.insert(values={'md5': md5}).execute()

def set_rating(path, value):
   upd = nodes_table.update()
   upd = upd.values(rating=value)
   upd = upd.where(nodes_table.c.path==path)
   upd.execute()

def expected_params(query_types):
   num = 0

   for type in query_types:

      if type == 'rating':
         num += 2

      if type == 'in_path':
         num += 1

      if type == 'md5':
         num += 1

      if type == 'date':
         num += 1

      if type == 'tag':
         num += 1

   return num

def from_incremental_query(sess, query):
   LOG.debug('parsing incremental query %r' % query)

   if not query or query == 'root' or query == '/':
      # list the available query schemes
      return [
            DummyNode('all'),
            DummyNode('date'),
            DummyNode('in_path'),
            DummyNode('md5'),
            DummyNode('named_queries'),
            DummyNode('rating'),
            DummyNode('tag'),
            ]
   else:
      if query.startswith('root'):
         query = query[5:]
      query_nodes = query.split('/')

   LOG.debug('Query nodes: %r' % query_nodes)

   # pop the query type off the beginning
   query_types = query_nodes.pop(0).lower()
   query_types = [x.strip() for x in query_types.split(',')]

   # handle flattened queries
   if query_nodes and query_nodes[-1] == "__flat__":
      query_nodes.pop()
      flatten = True
   else:
      flatten = False

   # Construct the different queries
   if len(query_types) == 1 and query_types[0] == 'all':
      return all(sess, query_nodes, flatten)

   if 'named_queries' in query_types and not query_nodes:
      nq_qry = sess.query(Query)
      nq_qry = nq_qry.filter( Query.label != None )
      nq_qry = nq_qry.order_by(Query.label)
      return [ DummyNode(x.label) for x in nq_qry.all() ]
   elif query_types[0] == 'named_queries':
      # fetch the saved query and replace the named query by that string
      query_name = query_nodes.pop(0)
      nq_qry = sess.query(Query)
      nq_qry = nq_qry.filter( Query.label == query_name ).first()
      if not nq_qry:
         return []

      prepend_nodes = nq_qry.query.split('/')
      query_nodes = prepend_nodes + query_nodes

   num_params = expected_params(query_types)
   if not query_nodes or len(query_nodes) < num_params:
      # no details known yet. Find appropriate queries
      output = []
      stmt = sess.query(Query.query)
      LOG.debug('Listing nodes starting with %r' % query)
      stmt = stmt.filter(query_table.c.query.startswith(query))
      stmt = stmt.order_by(query_table.c.query)
      for row in stmt:
         sub_nodes = row.query.split('/')
         # we're in the case where the initial nodes were empty. We only return
         # the next element
         output.append(DummyNode(sub_nodes[len(query_nodes)+1]))
      return output

   parent_uri = '/'.join(query_nodes[num_params:])

   parent_path = uri_to_ltree(parent_uri)
   depth = uri_depth(parent_uri)

   if flatten:
      stmt = sess.query(Node)
   else:
      stmt = sess.query(
            distinct(func.subpath(Node.path, 0, depth+1).label("subpath"))
            )

   stmt = stmt.filter( Node.path.op("<@")(parent_path) )

   # apply all filters in sequence
   for query_type in query_types:
      if query_type == 'date':
         stmt = dated(sess, stmt, parent_uri, query_nodes)

      if query_type == 'rating':
         stmt = rated(stmt, parent_uri, query_nodes)

      if query_type == 'md5':
         stmt = has_md5(stmt, parent_uri, query_nodes)

      if query_type == 'in_path':
         stmt = in_path(stmt, query_nodes)

      if query_type == 'tag':
         stmt = tagged(sess, stmt, parent_uri, query_nodes)

   if not flatten:
      stmt = stmt.subquery()
      qry = sess.query( Node )
      qry = qry.filter( Node.path.in_(stmt) )
      return qry

   return stmt

def map_to_fsold(query):
   """
   Remove any query specific elements, leaving only the fs-path
   """
   LOG.debug('Mapping to FS %r' % query)
   query_nodes = query.split("/")

   if not query_nodes:
      return None

   # pop the query type off the beginning
   query_types = query_nodes.pop(0).lower()
   query_types = [x.strip() for x in query_types.split(',')]
   chop_params = expected_params(query_types)+1
   out = '/' + '/'.join(query_nodes[chop_params:])
   LOG.info('Expected params: %d' % chop_params)
   LOG.info('remainder: %r' % query_nodes[chop_params:])

   return out

   return None

@memoized
def map_to_fs(sess, query):
   """
   Remove any query specific elements, leaving only the fs-path
   """
   LOG.debug('Mapping to FS %r' % query)
   if query[0] == '/':
      query = query[1:]
   query_nodes = query.split("/")

   if not query_nodes:
      return None

   # pop the query type off the beginning
   query_types = query_nodes[0].lower()
   query_types = [x.strip() for x in query_types.split(',')]

   LOG.debug('Query types: %r' % query_types)
   chop_params = expected_params(query_types)
   LOG.debug('Expected number of params: %d' % chop_params)

   map_nodes = query_nodes[chop_params+1:]

   # Windows adds a wildcard. We'll remove it again...
   if map_nodes and map_nodes[-1] == '*':
      map_nodes.pop()

   if not map_nodes:
      return '/'

   if map_nodes[0] == 'ROOT' and '__flat__' not in map_nodes:
      LOG.debug('normal mapping of %r ' % map_nodes)
      map_nodes.pop(0) # remove leading 'ROOT'

      LOG.info('remainder: %r' % map_nodes)
      out = '/' + '/'.join(map_nodes)
      return out

   elif map_nodes[-1] == '__flat__':
      return '/'

   elif "__flat__" in map_nodes:

      flat_pos = map_nodes.index('__flat__')
      mapping_base = query_nodes[0:query_nodes.index('__flat__')+1]
      md5name = map_nodes[-1] # in normal cases, we should only have one entry after __flat__. Which is it's whole purpose!

      LOG.debug('flattened mapping of %r ' % map_nodes)
      mapping_base = '/'.join(mapping_base)

      flatten_map = {}
      flatten_map[mapping_base] = {}
      stmt = from_incremental_query(sess, mapping_base)
      for node in stmt:
         flatten_map[mapping_base][node.md5name] = node.uri
      return flatten_map[mapping_base].get(md5name, None)

      #if mapping_base not in FLATTEN_MAP:
      #   LOG.debug('populating map for %r' % mapping_base)
      #   FLATTEN_MAP[mapping_base] = ({}, datetime.now())
      #   stmt = from_incremental_query(sess, mapping_base)
      #   for node in stmt:
      #      FLATTEN_MAP[mapping_base][0][node.md5name] = node.uri

      #return FLATTEN_MAP[mapping_base][0].get(md5name, None)

class DummyNode(object):

   def __init__(self, label):
      self.label = label

   def __repr__(self):
      return "<DummyNode %s %r>" % (
            self.is_dir() and "d" or "f",
            self.label)

   def is_dir(self):
      return True

   @property
   def basename(self):
      return self.label

class Node(DummyNode):

   def __init__(self, uri):
      self.path = uri_to_ltree(uri)
      self.uri = uri

   def __repr__(self):
      return "<Node %s %r>" % (
            self.is_dir() and "d" or "f",
            self.uri)

   def is_dir(self):
      return self.mimetype == "other/directory"

   @property
   def basename(self):
      if self.uri == '/':
         return 'ROOT'
      return basename(self.uri)

   @property
   def md5name(self):
      from hashlib import md5
      if self.uri == '/':
         return 'ROOT'

      extension_parts = self.uri.rsplit('.', 1)
      if len(extension_parts) > 1:
         ext = extension_parts[-1]
      else:
         ext = ""

      out = "%s.%s" % (md5(self.uri).hexdigest(), ext)
      return out

class Tag(object):

   @classmethod
   def find(self, sess, name):
      qry = sess.query(Tag)
      qry = qry.filter( Tag.name == name )
      return qry.first()

   def __init__(self, name):
      self.name = name

   def __repr__(self):
      return self.name

mapper(Tag, tag_table)

mapper(Node, nodes_table, properties={
   'tags': relation(Tag, secondary=node_has_tag_table, backref='nodes')
   })

