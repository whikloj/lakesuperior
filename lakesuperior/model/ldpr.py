import logging

from abc import ABCMeta
from collections import defaultdict
from copy import deepcopy
from itertools import accumulate, groupby
from pprint import pformat
from uuid import uuid4

import arrow
import rdflib

from flask import current_app, g, request
from rdflib import Graph
from rdflib.resource import Resource
from rdflib.namespace import RDF, XSD
from rdflib.term import URIRef, Literal

from lakesuperior.dictionaries.namespaces import ns_collection as nsc
from lakesuperior.dictionaries.srv_mgd_terms import  srv_mgd_subjects, \
        srv_mgd_predicates, srv_mgd_types
from lakesuperior.exceptions import *
from lakesuperior.store_layouts.ldp_rs.base_rdf_layout import BaseRdfLayout


def atomic(fn):
    '''
    Handle atomic operations in an RDF store.

    This wrapper ensures that a write operation is performed atomically. It
    also takes care of sending a message for each resource changed in the
    transaction.
    '''
    def wrapper(self, *args, **kwargs):
        request.changelog = []
        try:
            ret = fn(self, *args, **kwargs)
        except:
            self._logger.warn('Rolling back transaction.')
            self.rdfly.store.rollback()
            raise
        else:
            self._logger.info('Committing transaction.')
            self.rdfly.store.commit()
            for ev in request.changelog:
                self._logger.info('Message: {}'.format(pformat(ev)))
                self._send_event_msg(*ev)
            return ret

    return wrapper



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
    LDP_NR_TYPE = nsc['ldp'].NonRDFSource
    LDP_RS_TYPE = nsc['ldp'].RDFSource
    MBR_RSRC_URI = nsc['ldp'].membershipResource
    MBR_REL_URI = nsc['ldp'].hasMemberRelation
    RETURN_CHILD_RES_URI = nsc['fcrepo'].Children
    RETURN_INBOUND_REF_URI = nsc['fcrepo'].InboundReferences
    RETURN_SRV_MGD_RES_URI = nsc['fcrepo'].ServerManaged
    ROOT_NODE_URN = nsc['fcsystem'].root

    # Workflow type. Inbound means that the resource is being written to the
    # store, outbounnd is being retrieved for output.
    WRKF_INBOUND = '_workflow:inbound_'
    WRKF_OUTBOUND = '_workflow:outbound_'

    # Default user to be used for the `createdBy` and `lastUpdatedBy` if a user
    # is not provided.
    DEFAULT_USER = Literal('BypassAdmin')

    RES_CREATED = '_create_'
    RES_DELETED = '_delete_'
    RES_UPDATED = '_update_'

    RES_VER_CONT_LABEL = 'fcr:versions'

    protected_pred = (
        nsc['fcrepo'].created,
        nsc['fcrepo'].createdBy,
        nsc['ldp'].contains,
    )

    _logger = logging.getLogger(__name__)


    ## STATIC & CLASS METHODS ##

    @classmethod
    def outbound_inst(cls, uuid, repr_opts={}, **kwargs):
        '''
        Create an instance for retrieval purposes.

        This factory method creates and returns an instance of an LDPR subclass
        based on information that needs to be queried from the underlying
        graph store.

        N.B. The resource must exist.

        @param uuid UUID of the instance.
        '''
        cls._logger.info('Retrieving stored resource: {}'.format(uuid))
        imr_urn = nsc['fcres'][uuid] if uuid else cls.ROOT_NODE_URN

        imr = current_app.rdfly.extract_imr(imr_urn, **repr_opts)
        cls._logger.debug('Extracted graph: {}'.format(
                pformat(set(imr.graph))))
        rdf_types = set(imr.graph.objects(imr.identifier, RDF.type))

        if cls.LDP_NR_TYPE in rdf_types:
            from lakesuperior.model.ldp_nr import LdpNr
            cls._logger.info('Resource is a LDP-NR.')
            rsrc = LdpNr(uuid, repr_opts, **kwargs)
        elif cls.LDP_RS_TYPE in rdf_types:
            from lakesuperior.model.ldp_rs import LdpRs
            cls._logger.info('Resource is a LDP-RS.')
            rsrc = LdpRs(uuid, repr_opts, **kwargs)
        else:
            raise ResourceNotExistsError(uuid)

        # Sneak in the already extracted IMR to save a query.
        rsrc._imr = imr

        return rsrc


    @staticmethod
    def inbound_inst(uuid, content_length, mimetype, stream, **kwargs):
        '''
        Determine LDP type (and instance class) from request headers and body.

        This is used with POST and PUT methods.

        @param uuid (string) UUID of the resource to be created or updated.
        '''
        # @FIXME Circular reference.
        from lakesuperior.model.ldp_nr import LdpNr
        from lakesuperior.model.ldp_rs import Ldpc, LdpDc, LdpIc, LdpRs

        urn = nsc['fcres'][uuid]

        logger = __class__._logger

        if not content_length:
            # Create empty LDPC.
            logger.debug('No data received in request. '
                    'Creating empty container.')

            inst = Ldpc(uuid, provided_imr=Resource(Graph(), urn), **kwargs)

        elif __class__.is_rdf_parsable(mimetype):
            # Create container and populate it with provided RDF data.
            input_rdf = stream.read()
            provided_gr = Graph().parse(data=input_rdf,
                    format=mimetype, publicID=urn)
            logger.debug('Provided graph: {}'.format(
                    pformat(set(provided_gr))))
            local_gr = g.tbox.localize_graph(provided_gr)
            logger.debug('Parsed local graph: {}'.format(
                    pformat(set(local_gr))))
            provided_imr = Resource(local_gr, urn)

            # Determine whether it is a basic, direct or indirect container.
            if Ldpr.MBR_RSRC_URI in local_gr.predicates() and \
                    Ldpr.MBR_REL_URI in local_gr.predicates():
                if Ldpr.INS_CNT_REL_URI in local_gr.predicates():
                    cls = LdpIc
                else:
                    cls = LdpDc
            else:
                cls = Ldpc

            inst = cls(uuid, provided_imr=provided_imr, **kwargs)

            # Make sure we are not updating an LDP-RS with an LDP-NR.
            if inst.is_stored and inst.LDP_NR_TYPE in inst.ldp_types:
                raise IncompatibleLdpTypeError(uuid, mimetype)

            inst._check_mgd_terms(inst.provided_imr.graph)

        else:
            # Create a LDP-NR and equip it with the binary file provided.
            provided_imr = Resource(Graph(), urn)
            inst = LdpNr(uuid, stream=stream, mimetype=mimetype,
                    provided_imr=provided_imr, **kwargs)

            # Make sure we are not updating an LDP-NR with an LDP-RS.
            if inst.is_stored and inst.LDP_RS_TYPE in inst.ldp_types:
                raise IncompatibleLdpTypeError(uuid, mimetype)

        logger.info('Creating resource of type: {}'.format(
                inst.__class__.__name__))

        try:
            types = inst.types
        except:
            types = set()
        if nsc['fcrepo'].Pairtree in types:
            raise InvalidResourceError(inst.uuid)

        return inst


    @staticmethod
    def is_rdf_parsable(mimetype):
        '''
        Checks whether a MIME type support RDF parsing by a RDFLib plugin.

        @param mimetype (string) MIME type to check.
        '''
        try:
            rdflib.plugin.get(mimetype, rdflib.parser.Parser)
        except rdflib.plugin.PluginException:
            return False
        else:
            return True


    @staticmethod
    def is_rdf_serializable(mimetype):
        '''
        Checks whether a MIME type support RDF serialization by a RDFLib plugin

        @param mimetype (string) MIME type to check.
        '''
        try:
            rdflib.plugin.get(mimetype, rdflib.serializer.Serializer)
        except rdflib.plugin.PluginException:
            return False
        else:
            return True


    ## MAGIC METHODS ##

    def __init__(self, uuid, repr_opts={}, provided_imr=None, **kwargs):
        '''Instantiate an in-memory LDP resource that can be loaded from and
        persisted to storage.

        Persistence is done in this class. None of the operations in the store
        layout should commit an open transaction. Methods are wrapped in a
        transaction by using the `@atomic` decorator.

        @param uuid (string) UUID of the resource. If None (must be explicitly
        set) it refers to the root node. It can also be the full URI or URN,
        in which case it will be converted.
        @param repr_opts (dict) Options used to retrieve the IMR. See
        `parse_rfc7240` for format details.
        @Param provd_rdf (string) RDF data provided by the client in
        operations isuch as `PUT` or `POST`, serialized as a string. This sets
        the `provided_imr` property.
        '''
        self.uuid = g.tbox.uri_to_uuid(uuid) \
                if isinstance(uuid, URIRef) else uuid
        self.urn = nsc['fcres'][uuid] \
                if self.uuid else self.ROOT_NODE_URN
        self.uri = g.tbox.uuid_to_uri(self.uuid)

        self.rdfly = current_app.rdfly
        self.nonrdfly = current_app.nonrdfly

        self.provided_imr = provided_imr


    @property
    def rsrc(self):
        '''
        The RDFLib resource representing this LDPR. This is a live
        representation of the stored data if present.

        @return rdflib.resource.Resource
        '''
        if not hasattr(self, '_rsrc'):
            self._rsrc = self.rdfly.ds.resource(self.urn)

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
                self._logger.debug('IMR options: {}'.format(self._imr_options))
                imr_options = self._imr_options
            else:
                imr_options = {}
            options = dict(imr_options, strict=True)
            self._imr = self.rdfly.extract_imr(self.urn, **options)

        return self._imr


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
                self._logger.debug('IMR options: {}'.format(self._imr_options))
                imr_options = self._imr_options
            else:
                imr_options = {}
            options = dict(imr_options, strict=True)
            try:
                self._imr = self.rdfly.extract_imr(self.urn, **options)
            except ResourceNotExistsError:
                self._imr = Resource(Graph(), self.urn)
                for t in self.base_types:
                    self.imr.add(RDF.type, t)

        return self._imr


    @imr.deleter
    def imr(self):
        '''
        Delete in-memory buffered resource.
        '''
        delattr(self, '_imr')


    @property
    def out_graph(self):
        '''
        Retun a globalized graph of the resource's IMR.

        Internal URNs are replaced by global URIs using the endpoint webroot.
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
                or (
                    not t[1] in srv_mgd_predicates
                    and not (t[1] == RDF.type or t[2] in srv_mgd_types)
                )
            ):
                out_gr.add(t)

        return out_gr


    @property
    def version_info(self):
        '''
        Return version metadata (`fcr:versions`).
        '''
        rsp = self.rdfly.get_version_info(self.urn)

        return g.tbox.globalize_graph(rsp)


    @property
    def is_stored(self):
        if hasattr(self, '_imr'):
            return len(self.imr.graph) > 0
        else:
            return self.rdfly.ask_rsrc_exists(self.urn)


    #@property
    #def has_versions(self):
    #    '''
    #    Whether if a current resource has versions.

    #    @return boolean
    #    '''
    #    return bool(self.imr.value(nsc['fcrepo'].hasVersions, any=False))


    @property
    def types(self):
        '''All RDF types.

        @return set(rdflib.term.URIRef)
        '''
        if not hasattr(self, '_types'):
            if hasattr(self, 'imr') and len(self.imr.graph):
                imr = self.imr
            elif hasattr(self, 'provided_imr') and \
                    len(self.provided_imr.graph):
                imr = provided_imr

            self._types = set(imr.graph[self.urn : RDF.type])

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

        digest = self.imr.value(nsc['premis'].hasMessageDigest)
        if digest:
            etag = digest.identifier.split(':')[-1]
            out_headers['ETag'] = 'W/"{}"'.format(etag),

        last_updated_term = self.imr.value(nsc['fcrepo'].lastModified)
        if last_updated_term:
            out_headers['Last-Modified'] = arrow.get(last_updated_term)\
                .format('ddd, D MMM YYYY HH:mm:ss Z')

        for t in self.ldp_types:
            out_headers['Link'].append(
                    '{};rel="type"'.format(t.n3()))

        return out_headers


    def get(self):
        '''
        This gets the RDF metadata. The binary retrieval is handled directly
        by the route.
        '''
        return g.tbox.globalize_graph(self.out_graph)


    @atomic
    def post(self):
        '''
        https://www.w3.org/TR/ldp/#ldpr-HTTP_POST

        Perform a POST action after a valid resource URI has been found.
        '''
        return self._create_or_replace_rsrc(create_only=True)


    @atomic
    def put(self):
        '''
        https://www.w3.org/TR/ldp/#ldpr-HTTP_PUT
        '''
        return self._create_or_replace_rsrc()


    def patch(self, *args, **kwargs):
        raise NotImplementedError()


    @atomic
    def delete(self, inbound=True, delete_children=True, leave_tstone=True):
        '''
        https://www.w3.org/TR/ldp/#ldpr-HTTP_DELETE

        @param inbound (boolean) If specified, delete all inbound relationships
        as well. This is the default and is always the case if referential
        integrity is enforced by configuration.
        @param delete_children (boolean) Whether to delete all child resources.
        This is the default.
        '''
        refint = current_app.config['store']['ldp_rs']['referential_integrity']
        inbound = True if refint else inbound

        children = self.imr[nsc['ldp'].contains * '+'] \
                if delete_children else []

        ret = self._delete_rsrc(inbound, leave_tstone)

        for child_uri in children:
            child_rsrc = Ldpr.outbound_inst(
                g.tbox.uri_to_uuid(child_uri.identifier),
                repr_opts={'incl_children' : False})
            child_rsrc._delete_rsrc(inbound, leave_tstone,
                    tstone_pointer=self.urn)

        return ret


    @atomic
    def delete_tombstone(self):
        '''
        Delete a tombstone.

        N.B. This does not trigger an event.
        '''
        remove_trp = {
            (self.urn, RDF.type, nsc['fcsystem'].Tombstone),
            (self.urn, nsc['fcrepo'].created, None),
            (None, nsc['fcsystem'].tombstone, self.urn),
        }
        self.rdfly.modify_dataset(remove_trp)


    def get_version(self, ver_uid):
        '''
        Get a version by label.
        '''
        ver_gr = self.rdfly.get_version(self.urn, ver_uid)

        return g.tbox.globalize_graph(ver_gr)


    @atomic
    def create_version(self, ver_uid):
        '''
        Create a new version of the resource.

        NOTE: This creates an event only for the resource being updated (due
        to the added `hasVersion` triple and possibly to the `hasVersions` one)
        but not for the version being created.

        @param ver_uid Version ver_uid. If already existing, an exception is
        raised.
        '''
        # Create version resource from copying the current state.
        ver_add_gr = Graph()
        vers_uuid = '{}/{}'.format(self.uuid, self.RES_VER_CONT_LABEL)
        ver_uuid = '{}/{}'.format(vers_uuid, ver_uid)
        ver_urn = nsc['fcres'][ver_uuid]
        ver_add_gr.add((ver_urn, RDF.type, nsc['fcrepo'].Version))
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
                    nsc['premis'].hasMessageDigest,
                }
            ):
                pass
            else:
                ver_add_gr.add((
                        g.tbox.replace_term_domain(t[0], self.urn, ver_urn),
                        t[1], t[2]))

        self.rdfly.modify_dataset(
                add_trp=ver_add_gr, types={nsc['fcrepo'].Version})

        # Add version metadata.
        meta_add_gr = Graph()
        meta_add_gr.add((
            self.urn, nsc['fcrepo'].hasVersion, ver_urn))
        meta_add_gr.add(
                (ver_urn, nsc['fcrepo'].created, g.timestamp_term))
        meta_add_gr.add(
                (ver_urn, nsc['fcrepo'].hasVersionLabel, Literal(ver_uid)))

        self.rdfly.modify_dataset(
                add_trp=meta_add_gr, types={nsc['fcrepo'].Metadata})

        # Update resource.
        rsrc_add_gr = Graph()
        rsrc_add_gr.add((
            self.urn, nsc['fcrepo'].hasVersions, nsc['fcres'][vers_uuid]))

        self._modify_rsrc(self.RES_UPDATED, add_trp=rsrc_add_gr, notify=False)

        return g.tbox.uuid_to_uri(ver_uuid)


    @atomic
    def revert_to_version(self, ver_uid):
        '''
        Revert to a previous version.

        NOTE: this will create a new version.

        @param ver_uid (string) Version UID.
        '''
        # Create a backup snapshot.
        self.create_version(uuid4())

        ver_gr = self.rdfly.get_version(self.urn, ver_uid)
        revert_gr = Graph()
        for t in ver_gr:
            if t[1] not in srv_mgd_predicates and not(
                t[1] == RDF.type and t[2] in srv_mgd_types
            ):
                revert_gr.add((self.urn, t[1], t[2]))

        self.provided_imr = revert_gr.resource(self.urn)

        return self._create_or_replace_rsrc(create_only=False)


    ## PROTECTED METHODS ##

    def _create_or_replace_rsrc(self, create_only=False):
        '''
        Create or update a resource. PUT and POST methods, which are almost
        identical, are wrappers for this method.

        @param create_only (boolean) Whether this is a create-only operation.
        '''
        create = create_only or not self.is_stored

        self._add_srv_mgd_triples(create)
        self._ensure_single_subject_rdf(self.provided_imr.graph)
        ref_int = self.rdfly.config['referential_integrity']
        if ref_int:
            self._check_ref_int(ref_int)

        if create:
            ev_type = self._create_rsrc()
        else:
            ev_type = self._replace_rsrc()

        self._set_containment_rel()

        return ev_type


    def _create_rsrc(self):
        '''
        Create a new resource by comparing an empty graph with the provided
        IMR graph.
        '''
        self._modify_rsrc(self.RES_CREATED, add_trp=self.provided_imr.graph)

        return self.RES_CREATED


    def _replace_rsrc(self):
        '''
        Replace a resource.

        The existing resource graph is removed except for the protected terms.
        '''
        # The extracted IMR is used as a "minus" delta, so protected predicates
        # must be removed.
        for p in self.protected_pred:
            self.imr.remove(p)

        delta = self._dedup_deltas(self.imr.graph, self.provided_imr.graph)
        self._modify_rsrc(self.RES_UPDATED, *delta)

        # Reset the IMR because it has changed.
        delattr(self, 'imr')

        return self.RES_UPDATED


    def _delete_rsrc(self, inbound, leave_tstone=True, tstone_pointer=None):
        '''
        Delete a single resource and create a tombstone.

        @param inbound (boolean) Whether to delete the inbound relationships.
        @param tstone_pointer (URIRef) If set to a URN, this creates a pointer
        to the tombstone of the resource that used to contain the deleted
        resource. Otherwise the delete resource becomes a tombstone.
        '''
        self._logger.info('Removing resource {}'.format(self.urn))

        remove_trp = self.imr.graph
        add_trp = Graph()

        if leave_tstone:
            if tstone_pointer:
                add_trp.add((self.urn, nsc['fcsystem'].tombstone,
                        tstone_pointer))
            else:
                add_trp.add((self.urn, RDF.type, nsc['fcsystem'].Tombstone))
                add_trp.add((self.urn, nsc['fcrepo'].created, g.timestamp_term))
        else:
            self._logger.info('NOT leaving tombstone.')

        self._modify_rsrc(self.RES_DELETED, remove_trp, add_trp)

        if inbound:
            remove_trp = set()
            for ib_rsrc_uri in self.imr.graph.subjects(None, self.urn):
                remove_trp = {(ib_rsrc_uri, None, self.urn)}
                Ldpr(ib_rsrc_uri)._modify_rsrc(self.RES_UPDATED, remove_trp)

        return self.RES_DELETED


    def _create_version_container(self):
        '''
        Create the relationship with `fcr:versions` the first time a version is
        created.
        '''
        add_gr = Graph()
        add_gr.add((self.urn, nsc['fcrepo'].hasVersions,
                URIRef(str(self.urn) + '/fcr:versions')))

        self._modify_rsrc(self.RES_UPDATED, add_trp=add_gr)


    def _modify_rsrc(self, ev_type, remove_trp=Graph(), add_trp=Graph(),
                     notify=True):
        '''
        Low-level method to modify a graph for a single resource.

        This is a crucial point for messaging. Any write operation on the RDF
        store that needs to be notified should be performed by invoking this
        method.

        @param ev_type (string) The type of event (create, update, delete).
        @param remove_trp (rdflib.Graph) Triples to be removed.
        @param add_trp (rdflib.Graph) Triples to be added.
        '''
        # If one of the triple sets is not a graph, do a set merge and
        # filtering. This is necessary to support non-RDF terms (e.g.
        # variables).
        if not isinstance(remove_trp, Graph) or not isinstance(add_trp, Graph):
            if isinstance(remove_trp, Graph):
                remove_trp = set(remove_trp)
            if isinstance(add_trp, Graph):
                add_trp = set(add_trp)
            merge_gr = remove_trp | add_trp
            type = { trp[2] for trp in merge_gr if trp[1] == RDF.type }
            actor = { trp[2] for trp in merge_gr \
                    if trp[1] == nsc['fcrepo'].createdBy }
        else:
            merge_gr = remove_trp | add_trp
            type = merge_gr[self.urn : RDF.type]
            actor = merge_gr[self.urn : nsc['fcrepo'].createdBy]

        ret = self.rdfly.modify_dataset(remove_trp, add_trp)

        if notify and current_app.config.get('messaging'):
            request.changelog.append((set(remove_trp), set(add_trp), {
                'ev_type' : ev_type,
                'time' : g.timestamp,
                'type' : type,
                'actor' : actor,
            }))

        return ret


    def _ensure_single_subject_rdf(self, gr):
        '''
        Ensure that a RDF payload for a POST or PUT has a single resource.
        '''
        for s in set(gr.subjects()):
            if not s == self.urn:
                raise SingleSubjectError(s, self.uuid)


    def _check_ref_int(self, config):
        gr = self.provided_imr.graph

        for o in gr.objects():
            if isinstance(o, URIRef) and str(o).startswith(g.webroot)\
                    and not self.rdfly.ask_rsrc_exists(o):
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
        '''
        # @FIXME Need to be more consistent
        if getattr(self, 'handling', 'none') == 'none':
            return gr

        offending_subjects = set(gr.subjects()) & srv_mgd_subjects
        if offending_subjects:
            if self.handling=='strict':
                raise ServerManagedTermError(offending_subjects, 's')
            else:
                for s in offending_subjects:
                    self._logger.info('Removing offending subj: {}'.format(s))
                    gr.remove((s, None, None))

        offending_predicates = set(gr.predicates()) & srv_mgd_predicates
        if offending_predicates:
            if self.handling=='strict':
                raise ServerManagedTermError(offending_predicates, 'p')
            else:
                for p in offending_predicates:
                    self._logger.info('Removing offending pred: {}'.format(p))
                    gr.remove((None, p, None))

        offending_types = set(gr.objects(predicate=RDF.type)) & srv_mgd_types
        if offending_types:
            if self.handling=='strict':
                raise ServerManagedTermError(offending_types, 't')
            else:
                for t in offending_types:
                    self._logger.info('Removing offending type: {}'.format(t))
                    gr.remove((None, RDF.type, t))

        self._logger.debug('Sanitized graph: {}'.format(gr.serialize(
            format='turtle').decode('utf-8')))
        return gr


    def _sparql_delta(self, q):
        '''
        Calculate the delta obtained by a SPARQL Update operation.

        This is a critical component of the SPARQL query prcess and does a
        couple of things:

        1. It ensures that no resources outside of the subject of the request
        are modified (e.g. by variable subjects)
        2. It verifies that none of the terms being modified is server managed.

        This method extracts an in-memory copy of the resource and performs the
        query on that once it has checked if any of the server managed terms is
        in the delta. If it is, it raises an exception.

        NOTE: This only checks if a server-managed term is effectively being
        modified. If a server-managed term is present in the query but does not
        cause any change in the updated resource, no error is raised.

        @return tuple(rdflib.Graph) Remove and add graphs. These can be used
        with `BaseStoreLayout.update_resource` and/or recorded as separate
        events in a provenance tracking system.
        '''
        pre_gr = self.imr.graph

        post_gr = deepcopy(pre_gr)
        post_gr.update(q)

        remove_gr, add_gr = self._dedup_deltas(pre_gr, post_gr)

        #self._logger.info('Removing: {}'.format(
        #    remove_gr.serialize(format='turtle').decode('utf8')))
        #self._logger.info('Adding: {}'.format(
        #    add_gr.serialize(format='turtle').decode('utf8')))

        remove_gr = self._check_mgd_terms(remove_gr)
        add_gr = self._check_mgd_terms(add_gr)

        return remove_gr, add_gr


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

        self.provided_imr.set(nsc['fcrepo'].lastModified, g.timestamp_term)
        self.provided_imr.set(nsc['fcrepo'].lastModifiedBy, self.DEFAULT_USER)


    def _set_containment_rel(self):
        '''Find the closest parent in the path indicated by the UUID and
        establish a containment triple.

        E.g. if only urn:fcres:a (short: a) exists:
        - If a/b/c/d is being created, a becomes container of a/b/c/d. Also,
          pairtree nodes are created for a/b and a/b/c.
        - If e is being created, the root node becomes container of e.
        '''
        # @FIXME Circular reference.
        from lakesuperior.model.ldp_rs import Ldpc

        if self.urn == self.ROOT_NODE_URN:
            return
        elif '/' in self.uuid:
            # Traverse up the hierarchy to find the parent.
            parent_uri = self._find_parent_or_create_pairtree(self.uuid)
        else:
            parent_uri = self.ROOT_NODE_URN

        add_gr = Graph()
        add_gr.add((parent_uri, nsc['ldp'].contains, self.urn))
        parent_rsrc = Ldpc(parent_uri, repr_opts={
                'incl_children' : False}, handling='none')
        parent_rsrc._modify_rsrc(self.RES_UPDATED, add_trp=add_gr)

        # Direct or indirect container relationship.
        self._add_ldp_dc_ic_rel(parent_uri)


    def _find_parent_or_create_pairtree(self, uuid):
        '''
        Check the path-wise parent of the new resource. If it exists, return
        its URI. Otherwise, create pairtree resources up the path until an
        actual resource or the root node is found.

        @return rdflib.term.URIRef
        '''
        path_components = uuid.split('/')

         # If there is only on element, the parent is the root node.
        if len(path_components) < 2:
            return self.ROOT_NODE_URN

        # Build search list, e.g. for a/b/c/d/e would be a/b/c/d, a/b/c, a/b, a
        self._logger.info('Path components: {}'.format(path_components))
        fwd_search_order = accumulate(
            list(path_components)[:-1],
            func=lambda x,y : x + '/' + y
        )
        rev_search_order = reversed(list(fwd_search_order))

        cur_child_uri = nsc['fcres'][uuid]
        for cparent_uuid in rev_search_order:
            cparent_uri = nsc['fcres'][cparent_uuid]

            if self.rdfly.ask_rsrc_exists(cparent_uri):
                return cparent_uri
            else:
                self._create_path_segment(cparent_uri, cur_child_uri)
                cur_child_uri = cparent_uri

        return self.ROOT_NODE_URN


    def _dedup_deltas(self, remove_gr, add_gr):
        '''
        Remove duplicate triples from add and remove delta graphs, which would
        otherwise contain unnecessary statements that annul each other.
        '''
        return (
            remove_gr - add_gr,
            add_gr - remove_gr
        )


    def _create_path_segment(self, uri, child_uri):
        '''
        Create a path segment with a non-LDP containment statement.

        This diverges from the default fcrepo4 behavior which creates pairtree
        resources.

        If a resource such as `fcres:a/b/c` is created, and neither fcres:a or
        fcres:a/b exists, we have to create two "hidden" containment statements
        between a and a/b and between a/b and a/b/c in order to maintain the
        `containment chain.
        '''
        imr = Resource(Graph(), uri)
        imr.add(RDF.type, nsc['ldp'].Container)
        imr.add(RDF.type, nsc['ldp'].BasicContainer)
        imr.add(RDF.type, nsc['ldp'].RDFSource)
        imr.add(RDF.type, nsc['fcrepo'].Pairtree)
        imr.add(nsc['fcrepo'].contains, child_uri)

        # If the path segment is just below root
        if '/' not in str(uri):
            imr.graph.add((nsc['fcsystem'].root, nsc['fcrepo'].contains, uri))

        self.rdfly.modify_dataset(add_trp=imr.graph)


    def _add_ldp_dc_ic_rel(self, cont_uri):
        '''
        Add relationship triples from a parent direct or indirect container.

        @param cont_uri (rdflib.term.URIRef)  The container URI.
        '''
        cont_uuid = g.tbox.uri_to_uuid(cont_uri)
        cont_rsrc = Ldpr.outbound_inst(cont_uuid,
                repr_opts={'incl_children' : False})
        cont_p = set(cont_rsrc.imr.graph.predicates())
        add_gr = Graph()

        self._logger.info('Checking direct or indirect containment.')
        self._logger.debug('Parent predicates: {}'.format(cont_p))

        if self.MBR_RSRC_URI in cont_p and self.MBR_REL_URI in cont_p:
            s = g.tbox.localize_term(
                    cont_rsrc.imr.value(self.MBR_RSRC_URI).identifier)
            p = cont_rsrc.imr.value(self.MBR_REL_URI).identifier

            if cont_rsrc.imr[RDF.type : nsc['ldp'].DirectContainer]:
                self._logger.info('Parent is a direct container.')

                self._logger.debug('Creating DC triples.')
                add_gr.add((s, p, self.urn))

            elif cont_rsrc.imr[RDF.type : nsc['ldp'].IndirectContainer] \
                   and self.INS_CNT_REL_URI in cont_p:
                self._logger.info('Parent is an indirect container.')
                cont_rel_uri = cont_rsrc.imr.value(self.INS_CNT_REL_URI).identifier
                target_uri = self.provided_imr.value(cont_rel_uri).identifier
                self._logger.debug('Target URI: {}'.format(target_uri))
                if target_uri:
                    self._logger.debug('Creating IC triples.')
                    add_gr.add((s, p, target_uri))

        if len(add_gr):
            add_gr = self._check_mgd_terms(add_gr)
            self._logger.debug('Adding DC/IC triples: {}'.format(
                add_gr.serialize(format='turtle').decode('utf-8')))
            self._modify_rsrc(self.RES_UPDATED, add_trp=add_gr)


    def _send_event_msg(self, remove_trp, add_trp, metadata):
        '''
        Break down delta triples, find subjects and send event message.
        '''
        remove_grp = groupby(remove_trp, lambda x : x[0])
        remove_dict = { k[0] : k[1] for k in remove_grp }

        add_grp = groupby(add_trp, lambda x : x[0])
        add_dict = { k[0] : k[1] for k in add_grp }

        subjects = set(remove_dict.keys()) | set(add_dict.keys())
        for rsrc_uri in subjects:
            self._logger.info('subject: {}'.format(rsrc_uri))
            #current_app.messenger.send
