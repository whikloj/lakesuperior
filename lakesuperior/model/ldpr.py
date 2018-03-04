import logging

from abc import ABCMeta
from collections import defaultdict
from itertools import groupby
from uuid import uuid4

import arrow

from flask import current_app, g
from rdflib import Graph
from rdflib.resource import Resource
from rdflib.namespace import RDF
from rdflib.term import URIRef, Literal

from lakesuperior.env import env
from lakesuperior.globals import RES_CREATED, RES_DELETED, RES_UPDATED
from lakesuperior.dictionaries.namespaces import ns_collection as nsc
from lakesuperior.dictionaries.namespaces import ns_mgr as nsm
from lakesuperior.dictionaries.srv_mgd_terms import  srv_mgd_subjects, \
        srv_mgd_predicates, srv_mgd_types
from lakesuperior.exceptions import (RefIntViolationError,
        ResourceNotExistsError, ServerManagedTermError, TombstoneError)
from lakesuperior.model.ldp_factory import LdpFactory
from lakesuperior.store.ldp_rs.rsrc_centric_layout import VERS_CONT_LABEL


ROOT_UID = ''
ROOT_RSRC_URI = nsc['fcres'][ROOT_UID]

rdfly = env.app_globals.rdfly


class Ldpr(metaclass=ABCMeta):
    '''LDPR (LDP Resource).

    Definition: https://www.w3.org/TR/ldp/#ldpr-resource

    This class and related subclasses contain the implementation pieces of
    the vanilla LDP specifications. This is extended by the
    `lakesuperior.fcrepo.Resource` class.

    Inheritance graph: https://www.w3.org/TR/ldp/#fig-ldpc-types

    Note: Even though LdpNr (which is a subclass of Ldpr) handles binary files,
    it still has an RDF representation in the triplestore. Hence, some of the
    RDF-related methods are defined in this class rather than in the LdpRs
    class.

    Convention notes:

    All the methods in this class handle internal UUIDs (URN). Public-facing
    URIs are converted from URNs and passed by these methods to the methods
    handling HTTP negotiation.

    The data passed to the store layout for processing should be in a graph.
    All conversion from request payload strings is done here.
    '''

    EMBED_CHILD_RES_URI = nsc['fcrepo'].EmbedResources
    FCREPO_PTREE_TYPE = nsc['fcrepo'].Pairtree
    INS_CNT_REL_URI = nsc['ldp'].insertedContentRelation
    MBR_RSRC_URI = nsc['ldp'].membershipResource
    MBR_REL_URI = nsc['ldp'].hasMemberRelation
    RETURN_CHILD_RES_URI = nsc['fcrepo'].Children
    RETURN_INBOUND_REF_URI = nsc['fcrepo'].InboundReferences
    RETURN_SRV_MGD_RES_URI = nsc['fcrepo'].ServerManaged

    # Workflow type. Inbound means that the resource is being written to the
    # store, outbounnd is being retrieved for output.
    WRKF_INBOUND = '_workflow:inbound_'
    WRKF_OUTBOUND = '_workflow:outbound_'

    # Default user to be used for the `createdBy` and `lastUpdatedBy` if a user
    # is not provided.
    DEFAULT_USER = Literal('BypassAdmin')

    # RDF Types that populate a new resource.
    base_types = {
        nsc['fcrepo'].Resource,
        nsc['ldp'].Resource,
        nsc['ldp'].RDFSource,
    }

    # Predicates that do not get removed when a resource is replaced.
    protected_pred = (
        nsc['fcrepo'].created,
        nsc['fcrepo'].createdBy,
        nsc['ldp'].contains,
    )

    # Server-managed RDF types ignored in the RDF payload if the resource is
    # being created. N.B. These still raise an error if the resource exists.
    smt_allow_on_create = {
        nsc['ldp'].DirectContainer,
        nsc['ldp'].IndirectContainer,
    }

    _logger = logging.getLogger(__name__)


    ## MAGIC METHODS ##

    def __init__(self, uid, repr_opts={}, provided_imr=None, **kwargs):
        '''Instantiate an in-memory LDP resource that can be loaded from and
        persisted to storage.

        @param uid (string) uid of the resource. If None (must be explicitly
        set) it refers to the root node. It can also be the full URI or URN,
        in which case it will be converted.
        @param repr_opts (dict) Options used to retrieve the IMR. See
        `parse_rfc7240` for format details.
        @Param provd_rdf (string) RDF data provided by the client in
        operations such as `PUT` or `POST`, serialized as a string. This sets
        the `provided_imr` property.
        '''
        self.uid = rdfly.uri_to_uid(uid) \
                if isinstance(uid, URIRef) else uid
        self.uri = nsc['fcres'][uid]

        self.provided_imr = provided_imr


    @property
    def rsrc(self):
        '''
        The RDFLib resource representing this LDPR. This is a live
        representation of the stored data if present.

        @return rdflib.resource.Resource
        '''
        if not hasattr(self, '_rsrc'):
            self._rsrc = rdfly.ds.resource(self.uri)

        return self._rsrc


    @property
    def imr(self):
        '''
        Extract an in-memory resource from the graph store.

        If the resource is not stored (yet), a `ResourceNotExistsError` is
        raised.

        @return rdflib.resource.Resource
        '''
        if not hasattr(self, '_imr'):
            if hasattr(self, '_imr_options'):
                self._logger.debug(
                        'Getting RDF representation for resource /{}'
                        .format(self.uid))
                #self._logger.debug('IMR options: {}'.format(self._imr_options))
                imr_options = self._imr_options
            else:
                imr_options = {}
            options = dict(imr_options, strict=True)
            self._imr = rdfly.extract_imr(self.uid, **options)

        return self._imr


    @imr.setter
    def imr(self, v):
        '''
        Replace in-memory buffered resource.

        @param v (set | rdflib.Graph) New set of triples to populate the IMR
        with.
        '''
        if isinstance(v, Resource):
            v = v.graph
        self._imr = Resource(Graph(), self.uri)
        gr = self._imr.graph
        gr += v


    @imr.deleter
    def imr(self):
        '''
        Delete in-memory buffered resource.
        '''
        delattr(self, '_imr')


    @property
    def metadata(self):
        '''
        Get resource metadata.
        '''
        if not hasattr(self, '_metadata'):
            if hasattr(self, '_imr'):
                self._logger.info('Metadata is IMR.')
                self._metadata = self._imr
            else:
                self._logger.info('Getting metadata for resource /{}'
                        .format(self.uid))
                self._metadata = rdfly.get_metadata(self.uid)

        return self._metadata


    @metadata.setter
    def metadata(self, rsrc):
        '''
        Set resource metadata.
        '''
        if not isinstance(rsrc, Resource):
            raise TypeError('Provided metadata is not a Resource object.')
        self._metadata = rsrc


    @property
    def stored_or_new_imr(self):
        '''
        Extract an in-memory resource for harmless manipulation and output.

        If the resource is not stored (yet), initialize a new IMR with basic
        triples.

        @return rdflib.resource.Resource
        '''
        if not hasattr(self, '_imr'):
            if hasattr(self, '_imr_options'):
                #self._logger.debug('IMR options: {}'.format(self._imr_options))
                imr_options = self._imr_options
            else:
                imr_options = {}
            options = dict(imr_options, strict=True)
            try:
                self._imr = rdfly.extract_imr(self.uid, **options)
            except ResourceNotExistsError:
                self._imr = Resource(Graph(), self.uri)
                for t in self.base_types:
                    self.imr.add(RDF.type, t)

        return self._imr


    @property
    def out_graph(self):
        '''
        Retun a graph of the resource's IMR formatted for output.
        '''
        out_gr = Graph()

        for t in self.imr.graph:
            if (
                # Exclude digest hash and version information.
                t[1] not in {
                    nsc['premis'].hasMessageDigest,
                    nsc['fcrepo'].hasVersion,
                }
            ) and (
                # Only include server managed triples if requested.
                self._imr_options.get('incl_srv_mgd', True)
                or not self._is_trp_managed(t)
            ):
                out_gr.add(t)

        return out_gr


    @property
    def version_info(self):
        '''
        Return version metadata (`fcr:versions`).
        '''
        if not hasattr(self, '_version_info'):
            try:
                #@ TODO get_version_info should return a graph.
                self._version_info = rdfly.get_version_info(self.uid).graph
            except ResourceNotExistsError as e:
                self._version_info = Graph(identifer=self.uri)

        return self._version_info


    @property
    def version_uids(self):
        '''
        Return a generator of version UIDs (relative to their parent resource).
        '''
        gen = self.version_info[
                nsc['fcrepo'].hasVersion / nsc['fcrepo'].hasVersionLabel]

        return { str(uid) for uid in gen }


    @property
    def is_stored(self):
        if not hasattr(self, '_is_stored'):
            if hasattr(self, '_imr'):
                self._is_stored = len(self.imr.graph) > 0
            else:
                self._is_stored = rdfly.ask_rsrc_exists(self.uid)

        return self._is_stored


    @property
    def types(self):
        '''All RDF types.

        @return set(rdflib.term.URIRef)
        '''
        if not hasattr(self, '_types'):
            if len(self.metadata.graph):
                metadata = self.metadata
            elif getattr(self, 'provided_imr', None) and \
                    len(self.provided_imr.graph):
                metadata = self.provided_imr
            else:
                return set()

            self._types = set(metadata.graph[self.uri : RDF.type])

        return self._types


    @property
    def ldp_types(self):
        '''The LDP types.

        @return set(rdflib.term.URIRef)
        '''
        if not hasattr(self, '_ldp_types'):
            self._ldp_types = { t for t in self.types if nsc['ldp'] in t }

        return self._ldp_types


    ## LDP METHODS ##

    def head(self):
        '''
        Return values for the headers.
        '''
        out_headers = defaultdict(list)

        digest = self.metadata.value(nsc['premis'].hasMessageDigest)
        if digest:
            etag = digest.identifier.split(':')[-1]
            out_headers['ETag'] = 'W/"{}"'.format(etag),

        last_updated_term = self.metadata.value(nsc['fcrepo'].lastModified)
        if last_updated_term:
            out_headers['Last-Modified'] = arrow.get(last_updated_term)\
                .format('ddd, D MMM YYYY HH:mm:ss Z')

        for t in self.ldp_types:
            out_headers['Link'].append(
                    '{};rel="type"'.format(t.n3()))

        return out_headers


    def get_version(self, ver_uid, **kwargs):
        '''
        Get a version by label.
        '''
        return rdfly.extract_imr(self.uid, ver_uid, **kwargs).graph


    def create_or_replace_rsrc(self, create_only=False):
        '''
        Create or update a resource. PUT and POST methods, which are almost
        identical, are wrappers for this method.

        @param create_only (boolean) Whether this is a create-only operation.
        '''
        create = create_only or not self.is_stored

        self._add_srv_mgd_triples(create)
        #self._ensure_single_subject_rdf(self.provided_imr.graph)
        ref_int = rdfly.config['referential_integrity']
        if ref_int:
            self._check_ref_int(ref_int)

        rdfly.create_or_replace_rsrc(self.uid, self.provided_imr.graph)
        self.imr = self.provided_imr

        self._set_containment_rel()

        return RES_CREATED if create else RES_UPDATED
        #return self._head(self.provided_imr.graph)


    def put(self):
        '''
        https://www.w3.org/TR/ldp/#ldpr-HTTP_PUT
        '''
        return self.create_or_replace_rsrc()


    def patch(self, *args, **kwargs):
        raise NotImplementedError()


    def bury_rsrc(self, inbound, tstone_pointer=None):
        '''
        Delete a single resource and create a tombstone.

        @param inbound (boolean) Whether to delete the inbound relationships.
        @param tstone_pointer (URIRef) If set to a URN, this creates a pointer
        to the tombstone of the resource that used to contain the deleted
        resource. Otherwise the deleted resource becomes a tombstone.
        '''
        self._logger.info('Burying resource {}'.format(self.uid))
        # Create a backup snapshot for resurrection purposes.
        self.create_rsrc_snapshot(uuid4())

        remove_trp = {
                trp for trp in self.imr.graph
                if trp[1] != nsc['fcrepo'].hasVersion}

        if tstone_pointer:
            add_trp = {(self.uri, nsc['fcsystem'].tombstone,
                    tstone_pointer)}
        else:
            add_trp = {
                (self.uri, RDF.type, nsc['fcsystem'].Tombstone),
                (self.uri, nsc['fcrepo'].created, g.timestamp_term),
            }

        self._modify_rsrc(RES_DELETED, remove_trp, add_trp)

        if inbound:
            for ib_rsrc_uri in self.imr.graph.subjects(None, self.uri):
                remove_trp = {(ib_rsrc_uri, None, self.uri)}
                ib_rsrc = Ldpr(ib_rsrc_uri)
                # To preserve inbound links in history, create a snapshot
                ib_rsrc.create_rsrc_snapshot(uuid4())
                ib_rsrc._modify_rsrc(RES_UPDATED, remove_trp)

        return RES_DELETED


    def forget_rsrc(self, inbound=True):
        '''
        Remove all traces of a resource and versions.
        '''
        self._logger.info('Purging resource {}'.format(self.uid))
        refint = current_app.config['store']['ldp_rs']['referential_integrity']
        inbound = True if refint else inbound
        rdfly.forget_rsrc(self.uid, inbound)

        # @TODO This could be a different event type.
        return RES_DELETED


    def create_rsrc_snapshot(self, ver_uid):
        '''
        Perform version creation and return the version UID.
        '''
        # Create version resource from copying the current state.
        self._logger.info(
                'Creating version snapshot {} for resource {}.'.format(
                    ver_uid, self.uid))
        ver_add_gr = set()
        vers_uid = '{}/{}'.format(self.uid, VERS_CONT_LABEL)
        ver_uid = '{}/{}'.format(vers_uid, ver_uid)
        ver_uri = nsc['fcres'][ver_uid]
        ver_add_gr.add((ver_uri, RDF.type, nsc['fcrepo'].Version))
        for t in self.imr.graph:
            if (
                t[1] == RDF.type and t[2] in {
                    nsc['fcrepo'].Binary,
                    nsc['fcrepo'].Container,
                    nsc['fcrepo'].Resource,
                }
            ) or (
                t[1] in {
                    nsc['fcrepo'].hasParent,
                    nsc['fcrepo'].hasVersions,
                    nsc['fcrepo'].hasVersion,
                    nsc['premis'].hasMessageDigest,
                }
            ):
                pass
            else:
                ver_add_gr.add((
                        g.tbox.replace_term_domain(t[0], self.uri, ver_uri),
                        t[1], t[2]))

        rdfly.modify_rsrc(ver_uid, add_trp=ver_add_gr)

        # Update resource admin data.
        rsrc_add_gr = {
            (self.uri, nsc['fcrepo'].hasVersion, ver_uri),
            (self.uri, nsc['fcrepo'].hasVersions, nsc['fcres'][vers_uid]),
        }
        self._modify_rsrc(RES_UPDATED, add_trp=rsrc_add_gr, notify=False)

        return ver_uid


    def resurrect_rsrc(self):
        '''
        Resurrect a resource from a tombstone.

        @EXPERIMENTAL
        '''
        tstone_trp = set(rdfly.extract_imr(self.uid, strict=False).graph)

        ver_rsp = self.version_info.graph.query('''
        SELECT ?uid {
          ?latest fcrepo:hasVersionLabel ?uid ;
            fcrepo:created ?ts .
        }
        ORDER BY DESC(?ts)
        LIMIT 1
        ''')
        ver_uid = str(ver_rsp.bindings[0]['uid'])
        ver_trp = set(rdfly.get_metadata(self.uid, ver_uid).graph)

        laz_gr = Graph()
        for t in ver_trp:
            if t[1] != RDF.type or t[2] not in {
                nsc['fcrepo'].Version,
            }:
                laz_gr.add((self.uri, t[1], t[2]))
        laz_gr.add((self.uri, RDF.type, nsc['fcrepo'].Resource))
        if nsc['ldp'].NonRdfSource in laz_gr[: RDF.type :]:
            laz_gr.add((self.uri, RDF.type, nsc['fcrepo'].Binary))
        elif nsc['ldp'].Container in laz_gr[: RDF.type :]:
            laz_gr.add((self.uri, RDF.type, nsc['fcrepo'].Container))

        self._modify_rsrc(RES_CREATED, tstone_trp, set(laz_gr))
        self._set_containment_rel()

        return self.uri



    def create_version(self, ver_uid=None):
        '''
        Create a new version of the resource.

        NOTE: This creates an event only for the resource being updated (due
        to the added `hasVersion` triple and possibly to the `hasVersions` one)
        but not for the version being created.

        @param ver_uid Version ver_uid. If already existing, an exception is
        raised.
        '''
        if not ver_uid or ver_uid in self.version_uids:
            ver_uid = str(uuid4())

        return self.create_rsrc_snapshot(ver_uid)


    def revert_to_version(self, ver_uid, backup=True):
        '''
        Revert to a previous version.

        @param ver_uid (string) Version UID.
        @param backup (boolean) Whether to create a backup snapshot. Default is
        true.
        '''
        # Create a backup snapshot.
        if backup:
            self.create_version()

        ver_gr = rdfly.extract_imr(self.uid, ver_uid=ver_uid,
                incl_children=False)
        self.provided_imr = Resource(Graph(), self.uri)

        for t in ver_gr.graph:
            if not self._is_trp_managed(t):
                self.provided_imr.add(t[1], t[2])
            # @TODO Check individual objects: if they are repo-managed URIs
            # and not existing or tombstones, they are not added.

        return self.create_or_replace_rsrc(create_only=False)


    ## PROTECTED METHODS ##

    def _is_trp_managed(self, t):
        '''
        Whether a triple is server-managed.

        @return boolean
        '''
        return t[1] in srv_mgd_predicates or (
                t[1] == RDF.type and t[2] in srv_mgd_types)


    def _modify_rsrc(self, ev_type, remove_trp=set(), add_trp=set(),
             notify=True):
        '''
        Low-level method to modify a graph for a single resource.

        This is a crucial point for messaging. Any write operation on the RDF
        store that needs to be notified should be performed by invoking this
        method.

        @param ev_type (string) The type of event (create, update, delete).
        @param remove_trp (set) Triples to be removed.
        @param add_trp (set) Triples to be added.
        @param notify (boolean) Whether to send a message about the change.
        '''
        ret = rdfly.modify_rsrc(self.uid, remove_trp, add_trp)

        if notify and current_app.config.get('messaging'):
            self._enqueue_msg(ev_type, remove_trp, add_trp)

        return ret


    def _enqueue_msg(self, ev_type, remove_trp=None, add_trp=None):
        '''
        Sent a message about a changed (created, modified, deleted) resource.
        '''
        try:
            type = self.types
            actor = self.metadata.value(nsc['fcrepo'].createdBy)
        except (ResourceNotExistsError, TombstoneError):
            type = set()
            actor = None
            for t in add_trp:
                if t[1] == RDF.type:
                    type.add(t[2])
                elif actor is None and t[1] == nsc['fcrepo'].createdBy:
                    actor = t[2]

        changelog.append((set(remove_trp), set(add_trp), {
            'ev_type' : ev_type,
            'time' : g.timestamp,
            'type' : type,
            'actor' : actor,
        }))


    def _check_ref_int(self, config):
        gr = self.provided_imr.graph

        for o in gr.objects():
            if isinstance(o, URIRef) and str(o).startswith(nsc['fcres'])\
                    and not rdfly.ask_rsrc_exists(o):
                if config == 'strict':
                    raise RefIntViolationError(o)
                else:
                    self._logger.info(
                            'Removing link to non-existent repo resource: {}'
                            .format(o))
                    gr.remove((None, None, o))


    def _check_mgd_terms(self, gr):
        '''
        Check whether server-managed terms are in a RDF payload.

        @param gr (rdflib.Graph) The graph to validate.
        '''
        offending_subjects = set(gr.subjects()) & srv_mgd_subjects
        if offending_subjects:
            if self.handling=='strict':
                raise ServerManagedTermError(offending_subjects, 's')
            else:
                for s in offending_subjects:
                    self._logger.info('Removing offending subj: {}'.format(s))
                    gr.remove((s, None, None))

        offending_predicates = set(gr.predicates()) & srv_mgd_predicates
        # Allow some predicates if the resource is being created.
        if offending_predicates:
            if self.handling=='strict':
                raise ServerManagedTermError(offending_predicates, 'p')
            else:
                for p in offending_predicates:
                    self._logger.info('Removing offending pred: {}'.format(p))
                    gr.remove((None, p, None))

        offending_types = set(gr.objects(predicate=RDF.type)) & srv_mgd_types
        if not self.is_stored:
            offending_types -= self.smt_allow_on_create
        if offending_types:
            if self.handling=='strict':
                raise ServerManagedTermError(offending_types, 't')
            else:
                for t in offending_types:
                    self._logger.info('Removing offending type: {}'.format(t))
                    gr.remove((None, RDF.type, t))

        #self._logger.debug('Sanitized graph: {}'.format(gr.serialize(
        #    format='turtle').decode('utf-8')))
        return gr


    def _add_srv_mgd_triples(self, create=False):
        '''
        Add server-managed triples to a provided IMR.

        @param create (boolean) Whether the resource is being created.
        '''
        # Base LDP types.
        for t in self.base_types:
            self.provided_imr.add(RDF.type, t)

        # Message digest.
        cksum = g.tbox.rdf_cksum(self.provided_imr.graph)
        self.provided_imr.set(nsc['premis'].hasMessageDigest,
                URIRef('urn:sha1:{}'.format(cksum)))

        # Create and modify timestamp.
        if create:
            self.provided_imr.set(nsc['fcrepo'].created, g.timestamp_term)
            self.provided_imr.set(nsc['fcrepo'].createdBy, self.DEFAULT_USER)
        else:
            self.provided_imr.set(nsc['fcrepo'].created, self.metadata.value(
                    nsc['fcrepo'].created))
            self.provided_imr.set(nsc['fcrepo'].createdBy, self.metadata.value(
                    nsc['fcrepo'].createdBy))

        self.provided_imr.set(nsc['fcrepo'].lastModified, g.timestamp_term)
        self.provided_imr.set(nsc['fcrepo'].lastModifiedBy, self.DEFAULT_USER)


    def _set_containment_rel(self):
        '''Find the closest parent in the path indicated by the uid and
        establish a containment triple.

        Check the path-wise parent of the new resource. If it exists, add the
        containment relationship with this UID. Otherwise, create a container
        resource as the parent.
        This function may recurse up the path tree until an existing container
        is found.

        E.g. if only fcres:a exists:
        - If fcres:a/b/c/d is being created, a becomes container of
          fcres:a/b/c/d. Also, containers are created for fcres:a/b and
          fcres:a/b/c.
        - If fcres:e is being created, the root node becomes container of
          fcres:e.
        '''
        if '/' in self.uid:
            # Traverse up the hierarchy to find the parent.
            path_components = self.uid.split('/')
            cnd_parent_uid = '/'.join(path_components[:-1])
            if rdfly.ask_rsrc_exists(cnd_parent_uid):
                parent_rsrc = LdpFactory.from_stored(cnd_parent_uid)
                if nsc['ldp'].Container not in parent_rsrc.types:
                    raise InvalidResourceError(parent_uid,
                            'Parent {} is not a container.')

                parent_uid = cnd_parent_uid
            else:
                parent_rsrc = LdpFactory.new_container(cnd_parent_uid)
                # This will trigger this method again and recurse until an
                # existing container or the root node is reached.
                parent_rsrc.put()
                parent_uid = parent_rsrc.uid
        else:
            parent_uid = ROOT_UID

        add_gr = Graph()
        add_gr.add((nsc['fcres'][parent_uid], nsc['ldp'].contains, self.uri))
        parent_rsrc = LdpFactory.from_stored(
                parent_uid, repr_opts={'incl_children' : False},
                handling='none')
        parent_rsrc._modify_rsrc(RES_UPDATED, add_trp=add_gr)

        # Direct or indirect container relationship.
        self._add_ldp_dc_ic_rel(parent_rsrc)


    def _dedup_deltas(self, remove_gr, add_gr):
        '''
        Remove duplicate triples from add and remove delta graphs, which would
        otherwise contain unnecessary statements that annul each other.
        '''
        return (
            remove_gr - add_gr,
            add_gr - remove_gr
        )


    def _add_ldp_dc_ic_rel(self, cont_rsrc):
        '''
        Add relationship triples from a parent direct or indirect container.

        @param cont_rsrc (rdflib.resource.Resouce)  The container resource.
        '''
        cont_p = set(cont_rsrc.metadata.graph.predicates())

        self._logger.info('Checking direct or indirect containment.')
        self._logger.debug('Parent predicates: {}'.format(cont_p))

        add_trp = {(self.uri, nsc['fcrepo'].hasParent, cont_rsrc.uri)}

        if self.MBR_RSRC_URI in cont_p and self.MBR_REL_URI in cont_p:
            s = cont_rsrc.metadata.value(self.MBR_RSRC_URI).identifier
            p = cont_rsrc.metadata.value(self.MBR_REL_URI).identifier

            if cont_rsrc.metadata[RDF.type : nsc['ldp'].DirectContainer]:
                self._logger.info('Parent is a direct container.')

                self._logger.debug('Creating DC triples.')
                o = self.uri

            elif cont_rsrc.metadata[RDF.type : nsc['ldp'].IndirectContainer] \
                   and self.INS_CNT_REL_URI in cont_p:
                self._logger.info('Parent is an indirect container.')
                cont_rel_uri = cont_rsrc.metadata.value(
                        self.INS_CNT_REL_URI).identifier
                o = self.provided_imr.value(cont_rel_uri).identifier
                self._logger.debug('Target URI: {}'.format(o))
                self._logger.debug('Creating IC triples.')

            target_rsrc = LdpFactory.from_stored(rdfly.uri_to_uid(s))
            target_rsrc._modify_rsrc(RES_UPDATED, add_trp={(s, p, o)})

        self._modify_rsrc(RES_UPDATED, add_trp=add_trp)
