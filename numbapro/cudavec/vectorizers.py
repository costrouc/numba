import llvm.core as lc
from numbapro import cuda
from numbapro.npm import types, codegen
from numbapro.cudapy import codegen as cudapy_codegen
from numbapro.cudapy.compiler import to_ptx
from numbapro.cudapy.execution import CUDAKernel
from . import dispatch

vectorizer_stager_source = '''
def __vectorizer_stager__(%(args)s, __out__):
    __tid__ = __cuda__.grid(1)
    __out__[__tid__] = __core__(%(argitems)s)
'''

class CudaVectorize(object):
    def __init__(self, func):
        self.pyfunc = func
        self.kernelmap = {} # { arg_dtype: (return_dtype), cudakernel }

    def add(self, restype, argtypes):
        # compile core as device function
        cudevfn = cuda.jit(restype, argtypes,
                           device=True, inline=True)(self.pyfunc)

        # generate outer loop as kernel
        args = ['a%d' % i for i in range(len(argtypes))]
        fmts = dict(args = ', '.join(args),
                    argitems = ', '.join('%s[__tid__]' % i for i in args))
        kernelsource = vectorizer_stager_source % fmts
        glbl = self.pyfunc.func_globals
        glbl.update({'__cuda__': cuda, '__core__': cudevfn})
        exec kernelsource in glbl

        stager = glbl['__vectorizer_stager__']
        kargs = [a[:] for a in list(argtypes) + [restype]]
        kernel = cuda.jit(argtypes=kargs)(stager)

        argdtypes = tuple(t.get_dtype() for t in argtypes)
        resdtype = restype.get_dtype()
        self.kernelmap[tuple(argdtypes)] = resdtype, kernel

    def build_ufunc(self):
        return dispatch.CudaUFuncDispatcher(self.kernelmap)

#------------------------------------------------------------------------------
# Generalized CUDA ufuncs

class CudaGUFuncVectorize(object):

    def __init__(self, func, sig):
        self.pyfunc = func
        self.signature = sig
        self.kernelmap = {}  # { arg_dtype: (return_dtype), cudakernel }

    def add(self, argtypes, restype=None):
        cudevfn = cuda.jit(argtypes=argtypes,
                           device=True, inline=True)(self.pyfunc)

        lmod, lgufunc, outertys = build_gufunc_stager(cudevfn)

        ptx = to_ptx(lgufunc)
        kernel = CUDAKernel(lgufunc.name, ptx, outertys)
        kernel.bind()

        dtypes = tuple(t.dtype.get_dtype() for t in argtypes)
        self.kernelmap[tuple(dtypes[:-1])] = dtypes[-1], kernel
        
    def build_ufunc(self):
        return dispatch.CudaGUFuncDispatcher(self.kernelmap, self.signature)

def build_gufunc_stager(devfn):
    lmod, lfunc, return_type, args = devfn._npm_context_
    assert return_type is None
    outer_args = [types.arraytype(a.element, a.ndim + 1, a.order)
                  for a in args]

    # copy a new module
    lmod = lmod.clone()
    lfunc = lmod.get_function_named(lfunc.name)
    typer = codegen.TypeSetter(intp=tuple.__itemsize__ * 8)

    argtypes = [typer.to_llvm(t) for t in outer_args]
    fnty = lc.Type.function(lc.Type.void(), argtypes)
    lgufunc = lmod.add_function(fnty, name='gufunc_%s' % lfunc.name)

    builder = lc.Builder.new(lgufunc.append_basic_block(''))

    # allocate new array with one less dimension
    txf = cudapy_codegen.declare_sreg_util(lmod, cuda.threadIdx.x)
    bxf = cudapy_codegen.declare_sreg_util(lmod, cuda.blockIdx.x)
    bdf = cudapy_codegen.declare_sreg_util(lmod, cuda.blockDim.x)
    tx = builder.call(txf, ())
    bx = builder.call(bxf, ())
    bd = builder.call(bdf, ())
    tid = builder.add(tx, builder.mul(bx, bd))

    slices = []
    for ary, inner, outer in zip(lgufunc.args, lfunc.type.pointee.args,
                                  outer_args):
        slice = builder.alloca(inner.pointee)
        slices.append(slice)

        data = builder.load(codegen.gep(builder, ary, 0, 0))
        shapeptr = codegen.gep(builder, ary, 0, 1)

        shape = [builder.load(codegen.gep(builder, shapeptr, 0, ax))
                    for ax in range(outer.ndim)]

        strideptr = codegen.gep(builder, ary, 0, 2)
        strides = [builder.load(codegen.gep(builder, strideptr, 0, ax))
                    for ax in range(outer.ndim)]

        slice_data = get_slice_data(builder, data, shape, strides, outer.order,
                                    tid)

        builder.store(slice_data, codegen.gep(builder, slice, 0, 0))
        for i, s in enumerate(shape[1:]):
            builder.store(s, codegen.gep(builder, slice, 0, 1, i))
        for i, s in enumerate(strides[1:]):
            builder.store(s, codegen.gep(builder, slice, 0, 2, i))

    builder.call(lfunc, slices)
    builder.ret_void()

    lmod.verify()
    return lmod, lgufunc, outer_args


def get_slice_data(builder, data, shape, strides, order, index):
    intp = shape[0].type
    indices = [builder.zext(index, intp)]
    indices += [lc.Constant.null(intp) for i in range(len(shape) - 1)]
    return codegen.array_pointer(builder, data, shape, strides, order, indices)