from flask import current_app, g
from rdflib import Graph
from rdflib.plugins.sparql.algebra import translateUpdate
from rdflib.plugins.sparql.parser import parseUpdate

from lakesuperior.dictionaries.namespaces import ns_collection as nsc
from lakesuperior.model.ldpr import Ldpr

class LdpRs(Ldpr):
    '''LDP-RS (LDP RDF source).

    Definition: https://www.w3.org/TR/ldp/#ldprs
    '''
    def __init__(self, uuid, repr_opts={}, handling='lenient', **kwargs):
        '''
        Extends Ldpr.__init__ by adding LDP-RS specific parameters.

        @param handling (string) One of `strict`, `lenient` (the default) or
        `none`. `strict` raises an error if a server-managed term is in the
        graph. `lenient` removes all sever-managed triples encountered. `none`
        skips all server-managed checks. It is used for internal modifications.
        '''
        super().__init__(uuid, **kwargs)
        self.base_types = super().base_types | {
            nsc['fcrepo'].Container,
            nsc['ldp'].Container,
        }

        # provided_imr can be empty. If None, it is an outbound resource.
        if self.provided_imr is not None:
            self.workflow = self.WRKF_INBOUND
        else:
            self.workflow = self.WRKF_OUTBOUND
            self._imr_options = repr_opts

        self.handling = handling


    ## LDP METHODS ##

    def patch(self, update_str):
        '''
        Update an existing resource by applying a SPARQL-UPDATE query.

        @param update_str (string) SPARQL-Update staements.
        '''
        self.handling = 'lenient' # FCREPO does that and Hyrax requires it.
        self._logger.debug('Local update string: {}'.format(local_update_str))

        return self._sparql_update(local_update_str)


    def _sparql_update(self, update_str, notify=True):
        '''
        Apply a SPARQL update to a resource.

        The SPARQL string is validated beforehand to make sure that it does
        not contain server-managed terms.

        In theory, server-managed terms in DELETE statements are harmless
        because the patch is only applied over the user-provided triples, but
        at the moment those are also checked.
        '''
        # Parse the SPARQL update string and validate contents.
        qry_struct = translateUpdate(parseUpdate(update_str))
        check_ins_gr = Graph()
        check_del_gr = Graph()
        for stmt in qry_struct:
            try:
                check_ins_gr += set(stmt.insert.triples)
            except AttributeError:
                pass
            try:
                check_del_gr += set(stmt.delete.triples)
            except AttributeError:
                pass

        self._check_mgd_terms(check_ins_gr)
        self._check_mgd_terms(check_del_gr)

        self.rdfly.patch_rsrc(self.uid, update_str)

        if notify and current_app.config.get('messaging'):
            self._enqueue_msg(self.RES_UPDATED, check_del_gr, check_ins_gr)

        # @FIXME Ugly workaround until we find how to recompose a SPARQL query
        # string from a parsed query object.
        self.rdfly.clear_smt(self.uid)

        return self.RES_UPDATED


    #def _sparql_delta(self, q):
    #    '''
    #    Calculate the delta obtained by a SPARQL Update operation.

    #    This is a critical component of the SPARQL update prcess and does a
    #    couple of things:

    #    1. It ensures that no resources outside of the subject of the request
    #    are modified (e.g. by variable subjects)
    #    2. It verifies that none of the terms being modified is server managed.

    #    This method extracts an in-memory copy of the resource and performs the
    #    query on that once it has checked if any of the server managed terms is
    #    in the delta. If it is, it raises an exception.

    #    NOTE: This only checks if a server-managed term is effectively being
    #    modified. If a server-managed term is present in the query but does not
    #    cause any change in the updated resource, no error is raised.

    #    @return tuple(rdflib.Graph) Remove and add graphs. These can be used
    #    with `BaseStoreLayout.update_resource` and/or recorded as separate
    #    events in a provenance tracking system.
    #    '''
    #    self._logger.debug('Provided SPARQL query: {}'.format(q))
    #    pre_gr = self.imr.graph

    #    post_gr = pre_gr | Graph()
    #    post_gr.update(q)

    #    remove_gr, add_gr = self._dedup_deltas(pre_gr, post_gr)

    #    #self._logger.debug('Removing: {}'.format(
    #    #    remove_gr.serialize(format='turtle').decode('utf8')))
    #    #self._logger.debug('Adding: {}'.format(
    #    #    add_gr.serialize(format='turtle').decode('utf8')))

    #    remove_gr = self._check_mgd_terms(remove_gr)
    #    add_gr = self._check_mgd_terms(add_gr)

    #    return set(remove_gr), set(add_gr)



class Ldpc(LdpRs):
    '''LDPC (LDP Container).'''

    def __init__(self, uuid, *args, **kwargs):
        super().__init__(uuid, *args, **kwargs)
        self.base_types |= {
            nsc['fcrepo'].Container,
            nsc['ldp'].Container,
        }



class LdpBc(Ldpc):
    '''LDP-BC (LDP Basic Container).'''
    def __init__(self, uuid, *args, **kwargs):
        super().__init__(uuid, *args, **kwargs)
        self.base_types |= {
            nsc['ldp'].BasicContainer,
        }



class LdpDc(Ldpc):
    '''LDP-DC (LDP Direct Container).'''

    def __init__(self, uuid, *args, **kwargs):
        super().__init__(uuid, *args, **kwargs)
        self.base_types |= {
            nsc['ldp'].DirectContainer,
        }



class LdpIc(Ldpc):
    '''LDP-IC (LDP Indirect Container).'''

    def __init__(self, uuid, *args, **kwargs):
        super().__init__(uuid, *args, **kwargs)
        self.base_types |= {
            nsc['ldp'].IndirectContainer,
        }

