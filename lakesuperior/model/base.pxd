from lakesuperior.cy_include cimport cytpl as tpl

ctypedef tpl.tpl_bin Buffer

cdef bytes buffer_dump(Buffer* buf)
