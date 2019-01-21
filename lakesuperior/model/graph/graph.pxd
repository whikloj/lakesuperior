from cymem.cymem cimport Pool

from lakesuperior.cy_include cimport calg
from lakesuperior.model.base cimport Buffer
from lakesuperior.model.graph.triple cimport BufferTriple
from lakesuperior.model.structures.keyset cimport Keyset
from lakesuperior.store.ldp_rs.lmdb_triplestore cimport LmdbTriplestore
from lakesuperior.store.ldp_rs.lmdb_triplestore cimport TripleKey

# Lookup function that returns whether a triple contains a match pattern.
ctypedef bint (*lookup_fn_t)(
        const BufferTriple *trp, const Buffer *t1, const Buffer *t2)

ctypedef Buffer SPOBuffer[3]
ctypedef Buffer *BufferPtr

cdef:
    unsigned int term_hash_fn(const calg.SetValue data)
    bint buffer_cmp_fn(const calg.SetValue v1, const calg.SetValue v2)
    unsigned int trp_hash_fn(const calg.SetValue btrp)
    bint triple_cmp_fn(const calg.SetValue v1, const calg.SetValue v2)


cdef class SimpleGraph:
    cdef:
        calg.Set *_triples # Set of unique triples.
        calg.Set *_terms # Set of unique serialized terms.
        LmdbTriplestore store
        # Temp data pool. It gets managed with the object lifecycle via cymem.
        Pool _pool

        void _data_from_lookup(self, tuple trp_ptn, ctx=*) except *
        void _data_from_keyset(self, Keyset data) except *
        inline void _add_from_spok(self, const TripleKey spok) except *
        inline void _add_triple(
            self, Buffer *ss, Buffer *sp, Buffer *so
        ) except *
        int _add_or_get_term(self, Buffer **data) except -1
        set _data_as_set(self)

    cpdef void set(self, tuple trp) except *
    cpdef void remove_triples(self, pattern) except *
    cpdef object as_rdflib(self)
    cpdef set terms(self, str type)

cdef class Imr(SimpleGraph):
    cdef:
        readonly str uri

    cpdef as_rdflib(self)

