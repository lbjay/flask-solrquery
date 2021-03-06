# -*- coding: utf-8 -*-
"""
flask.ext.solr
~~~~~~~~~~~~~~

Provides solr search capabilities for you app
"""

__version__ = '0.02'
__versionfull__ = __version__

import re
import logging
import requests
from math import ceil
from copy import deepcopy

from flask import current_app, g
from werkzeug.local import LocalProxy
import signals

logger = logging.getLogger(__name__)

solr = LocalProxy(lambda: current_app.extensions['solr'])    

__all__ = [
           'FlaskSolrQuery',
           'SearchResponseMixin',
           'SearchRequest',
           'solr',
           'signals'
           ]

class FlaskSolrQuery(object):
    """
    Connection to a solr instance using SOLRQUERY_URL parameter defined
    in Flask configuration
    """
    
    def __init__(self, app=None, config=None):
        self.app = app
        self.config = config
        self.response_loader = None
        if app is not None:
            self.init_app(app, config)
            
    def init_app(self, app, config=None):
        "Initialize the solr extension"

        if not (config is None or isinstance(config, dict)):
            raise ValueError("`config` must be an instance of dict or None")

        if config is None:
            config = self.config
        if config is None:
            config = app.config

        config.setdefault("SOLRQUERY_URL", "http://localhost:8983/solr/collection1/select")
        config.setdefault("SOLRQUERY_KEEPALIVE", False)
        config.setdefault("SOLRQUERY_TIMEOUT", 10)
        config.setdefault("SOLRQUERY_HTTP_METHOD", "GET")
        config.setdefault("SOLRQUERY_EXTRA_PARAMS", None)

        self._set_session(config)
        self.request_http_method = config['SOLRQUERY_HTTP_METHOD']
        self.query_url = config['SOLRQUERY_URL']
        self.timeout = config['SOLRQUERY_TIMEOUT']
        self.extra_params = config['SOLRQUERY_EXTRA_PARAMS']
        self.response_loader = self._default_loader
        
        if not hasattr(app, 'extensions'):
            app.extensions = {}
        
        app.extensions['solr'] = self
        return self
    
    def _set_session(self, config):
        
        self.session = requests.Session()
        self.session.keep_alive = config['SOLRQUERY_KEEPALIVE']

    def _default_loader(self, json, request, **kwargs):
        return SearchResponseMixin(json, request=request)
    
    def response_callback(self, callback):
        self.response_loader = callback
        
    def add_request_adapter(self, scheme, adapter):
        self.session.mount(scheme, adapter)
        
    def set_defaults(self, req, query_url=None):
        # allow for overriding query url
        if query_url is None:
            req.url = self.query_url
        else:
            req.url = query_url
            
        # add any configured extra params that are sent with every request
        if self.extra_params is not None:
            extra = dict(self.extra_params)
            req.set_params(**extra)
        
        req.method=self.request_http_method
        return req
        
    def query(self, q, query_url=None, **kwargs):
        """Just a helper method for querying solr
        Creates a request object and sends it
        to the create_request() method. If you 
        need something extra, you can create the
        request object yourself
        """
        
        req = self.create_request(q, **kwargs)
        req = self.set_defaults(req, query_url)
        return self.get_response(req)

    def create_request(self, q=None, rows=None, start=None, sort=[], query_fields=None, 
              filter_queries=[], fields=[], facets=[], highlights=[], **kwargs):
        """
        q - search terms as a string (str)
        rows - number of results to return (int)
        start - index position in total result set to start with (int)
        sort - tuple in the form of (field, direction) where direction is 'desc' or 'asc'
        query_fields - value to pass to solr as the qf param (str)
        filter_queries - list of fq param values
        fields - list of fields to pass as the fl param
        facets - list of tuples of the form (field, limit, mincount, output key, prefix)
        highlights - list of tuples of the form (field, snippet count, fragment size)
        **kwargs - any additional query string params to pass directly to solr 
        """

        req = SearchRequest(q)
    
        if start is not None:
            req.set_start(start)
            
        if rows is not None:
            req.set_rows(rows)
        
        for sort_field in sort:
            req.add_sort(*sort_field)

        for fq in filter_queries:
            req.add_filter_query(fq)

        if fields:
            req.set_fields(fields)
        
        if query_fields:
            req.set_query_fields(query_fields)
    
        for facet in facets:
            req.add_facet(*facet)
            
        for hl in highlights:
            req.add_highlight(*hl)
        
        # pass any additional kwargs on to the request params
        if len(kwargs):
            req.set_params(**kwargs)
            
        return req        
    
    def get_response(self, req):
        
        prepared_req = req.prepare()
        if 'Content-Type' not in prepared_req.headers:
          prepared_req.headers = {'Content-Type':'application/x-www-form-urlencoded'}
        try:
            http_resp = self.session.send(prepared_req, timeout=self.timeout)
            resp = self.response_loader(http_resp.json(), request=req, http_response=http_resp)
            signals.search_signal.send(self, response=resp)
            return resp
        except requests.RequestException, e:
            error_msg = "Something blew up when querying solr: %s; request url: %s" % \
                             (e, prepared_req.url)
            logger.error(error_msg)
            signals.error_signal.send(self, exc=e, request=req)
            raise

class SearchRequest(object):
    """
    Basically just an interface to a dict (SearchParams) of
    params to be sent to solr
    """
    def __init__(self, q, **kwargs):
        self.q = q
        self.params = SearchParams(q=q, **kwargs)
        
        #these are the request params (not exposed
        #directly, since this class encapsulates only
        #solr parameters (but they should be available
        #in case you need to change them
        self.url = None
        self.method = None
        self.headers=None
        self.files=None
        self.data=None
        self.auth=None
        self.cookies=None
        self.hooks=None
                
        
    def prepare(self, url=None, 
                method=None,
                headers=None,
                files=None,
                data=None,
                auth=None,
                cookies=None,
                hooks=None):
        
        """Prepares the Requests request - this will be
        send to the remote web service. Beware, if method
        is GET, then data cannot be sent!
        """
        
        if (method or self.method) == 'GET' and (data or self.data):
            raise Exception("You must set method to POST if you are sending data")
        
        r = requests.Request(method=method or self.method or 'GET', 
                             url=url or self.url, 
                             params=self.params.get_dict(),
                             headers=headers or self.headers,
                             files=files or self.files,
                             data=data or self.data,
                             auth=auth or self.auth,
                             cookies=cookies or self.cookies, 
                             hooks=hooks or self.hooks)
        
        self.prepared = r.prepare()
        self.url = self.prepared.url
        return self.prepared
        
    def set_params(self, **kwargs):
        self.params.update(**kwargs)
        return self
        
    def set_format(self, fmt):
        self.params.wt = fmt
        return self
        
    def set_rows(self, rows):
        self.params.rows = rows
        return self
        
    def set_start(self, start):
        self.params.start = start
        return self
        
    def set_fields(self, fields):
        fields = list(set(fields))
        self.params.fl = ','.join(fields)
        return self
        
    def get_fields(self):
        if self.params.has_key('fl'):
            return self.params.fl.split(',')
        return []
        
    def set_query_fields(self, query_fields):
        self.params.qf = query_fields
        return self

    def set_sort(self, sort_field, direction):
        self.params.sort = "%s %s" % (sort_field, direction)
        return self
        
    def add_sort(self, sort_field, direction):
        if not self.params.has_key('sort'):
            self.set_sort(sort_field, direction)
        else:
            self.params.sort += ',%s %s' % (sort_field, direction)
        return self
        
    def get_sort(self):
        sort = []
        if self.params.has_key('sort'):
            return [tuple(x.split()) for x in self.params.sort.split(',')]
    
    def set_hlq(self, hlq):
        self.params['hl.q'] = hlq
        return self
        
    def add_filter_query(self, fq):
        self.params.append('fq', fq)
        return self
        
    def get_filter_queries(self):
        return self.params.get('fq', [])
        
    def add_facet(self, field, limit=None, mincount=None, output_key=None, prefix=None):
        self.params['facet'] = "true"
        self.params.setdefault('facet.field', [])
        if output_key:
            self.params.append('facet.field', "{!ex=dt key=%s}%s" % (output_key, field))
        else:
            self.params.append('facet.field', field)
        if limit:
            self.params['f.%s.facet.limit' % field] = limit
        if mincount:
            self.params['f.%s.facet.mincount' % field] = mincount
        if prefix:
            self.params['f.%s.facet.prefix' % field] = prefix
        return self
            
    def facets_on(self):
        return self.params.facet and True or False
    
    def add_facet_prefix(self, field, prefix):
        self.params['f.%s.facet.prefix' % field] = prefix
    
    def get_facets(self):
        facets = []
        if self.facets_on():
            for fl in self.params.get('facet.field', []):
                if fl.startswith('{!ex=dt'):
                    m = re.search("key=(\w+)}(\w+)", fl)
                    output_key, fl = m.groups()
                else:
                    output_key = None
                limit = self.params.get('f.%s.facet.limit' % fl, None)
                mincount = self.params.get('f.%s.facet.mincount' % fl, None)
                prefix = self.params.get('f.%s.facet.prefix' % fl, None)
                facets.append((fl, limit, mincount, output_key, prefix))
        return facets
    
    def add_highlight(self, field, count=None, fragsize=None):
        self.params['hl'] = "true"
        if not self.params.has_key('hl.fl'):
            self.params['hl.fl'] = field
        elif field not in self.params['hl.fl'].split(','):
            self.params['hl.fl'] += ',' + field
        if count:
            self.params['f.%s.hl.snippets' % field] = count
        if fragsize:
            self.params['f.%s.hl.fragsize' % field] = fragsize
        return self
            
    def highlights_on(self):
        return self.params.hl and True or False
    
    def get_highlights(self):
        highlights = []
        if self.highlights_on() and self.params.has_key('hl.fl'):
            for fl in self.params.get('hl.fl').split(','):
                count = self.params.get('f.%s.hl.snippets' % fl, None)
                fragsize = self.params.get('f.%s.hl.fragsize' % fl, None)
                highlights.append((fl, count, fragsize))
        return highlights
    
    def get_param(self, param_name):
        return self.params.get(param_name, None)
    
class SearchParams(dict):
    """
    Simple dictionary wrapper that allows some keys to contain lists
    of values
    """    
    def __init__(self, **kwargs):
        self.update(wt='json', **kwargs)

    def __getattr__(self, key):
        if not self.has_key(key):
            return None
        return dict.__getitem__(self, key)

    def __setattr__(self, key, val):
        dict.__setitem__(self, key, val)

    def __repr__(self):
        dictrepr = dict.__repr__(self)
        return '%s(%s)' % (type(self).__name__, dictrepr)
    
    def get_dict(self):
        return dict.copy(self)
    
    def update(self, *args, **kwargs):
        for k, v in dict(*args, **kwargs).iteritems():
            self[k] = v
            
    def append(self, key, val):
        self.setdefault(key, [])
        if val not in self[key]:
            self[key].append(val)
                                
class SearchResponseMixin(object):
    """
    Wrapper for the response data returned by solr
    """
    
    def __init__(self, raw, request=None):
        self.raw = deepcopy(raw)
        self.request = request
        
    def is_error(self):
        return self.raw.get('responseHeader',{}).get('status', False)
    
    def get_error(self):
        """Function that returns the raw error message"""
        return self.raw.get('error', {}).get('msg', None)
    
    def raw_response(self):
        return self.raw
    
    def get_docset(self):
        if self.raw.has_key('response'):
            docset = self.raw['response'].get('docs', [])
            if self.request.highlights_on() and self.raw.has_key('highlighting'):
                for doc in docset:
                    doc['highlights'] = self.raw['highlighting'][doc['id']]
            return docset
        else:
            return []
    
    def get_doc(self, idx):
        docset = self.get_docset()
        return docset[idx]
            
    def get_doc_values(self, field, start=0, stop=None):
        docs = self.get_docset()
        return [x.get(field, None) for x in docs[int(start):int(stop)]]
    
    def get_all_facets(self):
        if not self.request.facets_on():
            return {}
        return self.raw.get('facet_counts',{})
    
    def get_all_facet_fields(self):
        return self.get_all_facets().get('facet_fields',{})
    
    def get_query(self):
        return self.raw.get('responseHeader',{}).get('params',{}).get('q')
    
    def get_count(self):
        """
        Returns number of documents in current response
        """
        if self.raw.has_key('response'):
            return len(self.raw['response']['docs'])
        else:
            return 0
    
    def get_hits(self):
        """
        Returns the total number of record found
        """
        return self.raw.get('response',{}).get('numFound',0)
    
    def get_start_count(self):
        """
        Returns the number of the first record in the 
        response compared to the total number
        """
        return self.raw.get('response',{}).get('start',0)
    
    def get_pagination(self, rows_per_page=10, max_pagination_len=5):
        """
        Returns a dictionary containing all the informations
        about the status of the pagination 
        """
        
        if not hasattr(self, 'pagination'):
            num_total_pages = int(ceil(float(self.get_hits()) / float(rows_per_page)))
            current_page = (int(self.get_start_count()) / rows_per_page) + 1
            max_num_pages_before = int(ceil(min(max_pagination_len, num_total_pages) / 2.0)) - 1
            max_num_pages_after = int(min(max_pagination_len, num_total_pages)) / 2
            distance_to_1 = current_page - 1
            distance_to_max = num_total_pages - current_page
            num_pages_before = min(distance_to_1, max_num_pages_before)
            num_pages_after = min(distance_to_max, max_num_pages_after)
            if num_pages_before < max_num_pages_before:
                num_pages_after += max_num_pages_before - num_pages_before
            if num_pages_after < max_num_pages_after:
                num_pages_before += max_num_pages_after - num_pages_after 
            pages_before = sorted([current_page - i for i in range(1, num_pages_before+1)])
            pages_after = sorted([current_page + i for i in range(1, num_pages_after+1)])
            self.pagination = {
                   'max_pagination_len':max_pagination_len ,
                   'num_total_pages': num_total_pages,
                   'current_page': current_page,
                   'pages_before': pages_before,
                   'pages_after': pages_after,       
            }

        return self.pagination
    
    def get_qtime(self):
        return self.raw.get('responseHeader',{}).get('QTime')
    
