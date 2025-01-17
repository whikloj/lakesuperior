import logging

from pprint import pformat
from uuid import uuid4

from rdflib.resource import Resource
from rdflib.namespace import RDF

from lakesuperior import env
from lakesuperior import exceptions as exc
from lakesuperior.model.ldp.ldpr import Ldpr
from lakesuperior.model.ldp.ldp_nr import LdpNr
from lakesuperior.model.ldp.ldp_rs import LdpRs, Ldpc, LdpDc, LdpIc
from lakesuperior.config_parser import config
from lakesuperior.dictionaries.namespaces import ns_collection as nsc
from lakesuperior.model.rdf.graph import Graph, from_rdf
from lakesuperior.util.toolbox import rel_uri_to_urn


LDP_NR_TYPE = nsc['ldp'].NonRDFSource
LDP_RS_TYPE = nsc['ldp'].RDFSource

rdfly = env.app_globals.rdfly
logger = logging.getLogger(__name__)


class LdpFactory:
    """
    Generate LDP instances.
    The instance classes are based on provided client data or on stored data.
    """
    @staticmethod
    def new_container(uid):
        if not uid.startswith('/') or uid == '/':
            raise exc.InvalidResourceError(uid)
        if rdfly.ask_rsrc_exists(uid):
            raise exc.ResourceExistsError(uid)
        rsrc = Ldpc(uid, provided_imr=Graph(uri=nsc['fcres'][uid]))

        return rsrc


    @staticmethod
    def from_stored(uid, ver_label=None, repr_opts={}, strict=True, **kwargs):
        """
        Create an instance for retrieval purposes.

        This factory method creates and returns an instance of an LDPR subclass
        based on information that needs to be queried from the underlying
        g"http://api.edu/location/id/1234/thumbnail"raph store.

        N.B. The resource must exist.

        :param str uid: UID of the instance.
        """
        # This will blow up if strict is True and the resource is a tombstone.
        rsrc_meta = rdfly.get_metadata(uid, strict=strict)
        rdf_types = rsrc_meta[nsc['fcres'][uid] : RDF.type]

        if LDP_NR_TYPE in rdf_types:
            logger.info('Resource is a LDP-NR.')
            cls = LdpNr
        elif LDP_RS_TYPE in rdf_types:
            logger.info('Resource is a LDP-RS.')
            cls = LdpRs
        else:
            raise exc.ResourceNotExistsError(uid)

        rsrc = cls(uid, repr_opts, **kwargs)
        # Sneak in the already extracted metadata to save a query.
        rsrc._metadata = rsrc_meta

        return rsrc


    @staticmethod
    def from_provided(
            uid, mimetype=None, stream=None, graph=None, rdf_data=None,
            rdf_fmt=None, **kwargs):
        r"""
        Create and LDPR instance from provided data.

        the LDP class (LDP-RS, LDP_NR, etc.) is determined by the contents
        passed.

        :param str uid: UID of the resource to be created or updated.
        :param str mimetype: The provided content MIME type. If this is
            specified the resource is considered a LDP-NR and a ``stream``
            *must* be provided.
        :param IOStream stream: The provided data stream.
        :param rdflib.Graph graph: Initial graph to populate the
            resource with. This can be used for LDP-RS and LDP-NR types alike.
        :param bytes rdf_data: Serialized RDF to build the initial graph.
        :param str rdf_fmt: Serialization format of RDF data.
        :param \*\*kwargs: Arguments passed to the LDP class constructor.

        :raise ValueError: if ``mimetype`` is specified but no data stream is
            provided.
        """
        uri = nsc['fcres'][uid]
        if rdf_data:
            try:
                provided_imr = from_rdf(
                        uri=uri, data=rdf_data,
                        format=rdf_fmt, publicID=nsc['fcres'][uid])
            except Exception as e:
                raise exc.RdfParsingError(rdf_fmt, str(e))

        elif graph:
            provided_imr = Graph(
                uri=uri, data={
                    (rel_uri_to_urn(s, uid), p, rel_uri_to_urn(o, uid))
                    for s, p, o in graph
                }
            )
        else:
            provided_imr = Graph(uri=uri)

        #logger.debug('Provided graph: {}'.format(
        #        pformat(set(provided_imr))))

        if stream is None:
            # Resource is a LDP-RS.
            if mimetype:
                raise ValueError(
                    'Binary stream must be provided if mimetype is specified.')

            # Determine whether it is a basic, direct or indirect container.
            if provided_imr[nsc['rdf'].type] == nsc['ldp'].IndirectContainer:
                cls = LdpIc
            elif provided_imr[nsc['rdf'].type] == nsc['ldp'].DirectContainer:
                cls = LdpDc
            else:
                cls = Ldpc

            inst = cls(uid, provided_imr=provided_imr, **kwargs)

            # Make sure we are not updating an LDP-NR with an LDP-RS.
            if inst.is_stored and LDP_NR_TYPE in inst.ldp_types:
                raise exc.IncompatibleLdpTypeError(uid, mimetype)

            if kwargs.get('handling', 'strict') != 'none':
                inst.check_mgd_terms(inst.provided_imr)

        else:
            # Resource is a LDP-NR.
            if not mimetype:
                mimetype = 'application/octet-stream'

            inst = LdpNr(uid, stream=stream, mimetype=mimetype,
                    provided_imr=provided_imr, **kwargs)

            # Make sure we are not updating an LDP-RS with an LDP-NR.
            if inst.is_stored and LDP_RS_TYPE in inst.ldp_types:
                raise exc.IncompatibleLdpTypeError(uid, mimetype)

        logger.debug('Creating resource of type: {}'.format(
                inst.__class__.__name__))

        return inst


    @staticmethod
    def mint_uid(parent_uid, path=None):
        """
        Mint a new resource UID based on client directives.

        This method takes a parent ID and a tentative path and returns an LDP
        resource UID.

        This may raise an exception resulting in a 404 if the parent is not
        found or a 409 if the parent is not a valid container.

        :param str parent_uid: UID of the parent resource. It must be an
            existing LDPC.
        :param str path: path to the resource, relative to the parent.

        :rtype: str
        :return: The confirmed resource UID. This may be different from
            what has been indicated.
        """
        if path and path.startswith('/'):
            raise ValueError('Slug cannot start with a slash.')
        # Shortcut!
        if not path and parent_uid == '/':
            return f'/{uuid4()}'

        if not parent_uid.startswith('/'):
            raise ValueError('Invalid parent UID: {}'.format(parent_uid))

        parent = LdpFactory.from_stored(parent_uid)
        if nsc['ldp'].Container not in parent.types:
            raise exc.InvalidResourceError(parent_uid,
                    'Parent {} is not a container.')

        pfx = parent_uid.rstrip('/') + '/'
        if path:
            cnd_uid = pfx + path
            if not rdfly.ask_rsrc_exists(cnd_uid):
                return cnd_uid

        return f'{pfx}{uuid4()}'


