import itertools
from logging import error

import numpy
import pycuda.gpuarray as gpuarray
import pycuda.driver as cuda
from pycuda.compiler import SourceModule
from pycuda.tools import DeviceData

import tigger.cluda as cluda
import tigger.cluda.dtypes as dtypes
from tigger.helpers import factors, wrap_in_tuple, product
from tigger.cluda.kernel import render_prelude, render_template_source
from tigger.cluda.vsize import VirtualSizes, render_stub_vsize_funcs


cuda.init()

API_ID = cluda.API_CUDA


def get_platforms():
    # For CUDA, there's only one platform
    return [Platform()]


class Platform:
    """
    Mimics pyopencl.Platform
    """

    name = "nVidia CUDA"
    vendor = "nVidia"
    version = ".".join(str(x) for x in cuda.get_version())

    def get_devices(self):
        return [Device(num) for num in range(cuda.Device.count())]

    def __str__(self):
        return self.name + " " + self.version


class Device(cuda.Device):

    def __init__(self, device_num):
        cuda.Device.__init__(self, device_num)
        self.name = self.name()


class Context:

    @classmethod
    def create(cls, device=None, **kwds):

        if device is None:
            platform = get_platforms()[0]
            device = platform.get_devices()[0]

        ctx = device.make_context()
        kwds['owns_context'] = True
        return cls(ctx, **kwds)

    def __init__(self, context, queue=None, fast_math=True, async=True, owns_context=False):
        self.api = cluda.api(API_ID)
        self._fast_math = fast_math
        self._context = context
        self._async = async
        self.device_params = DeviceParameters(context.get_device())

        self._stream = self.create_queue() if queue is None else queue
        self._released = False if owns_context else True

    def override_device_params(self, **kwds):
        for kwd in kwds:
            if hasattr(self.device_params, kwd):
                setattr(self.device_params, kwd, kwds[kwd])
            else:
                raise ValueError("Device parameter " + str(kwd) + " does not exist")

    def create_queue(self):
        return cuda.Stream()

    def supports_dtype(self, dtype):
        if dtypes.is_double(dtype):
            major, minor = self._context.get_device().compute_capability()
            return (major == 1 and minor == 3) or major >= 2
        else:
            return True

    def allocate(self, shape, dtype):
        return gpuarray.GPUArray(shape, dtype=dtype)

    def empty_like(self, arr):
        return self.allocate(arr.shape, arr.dtype)

    def to_device(self, arr, dest=None):
        if dest is None:
            arr_device = self.empty_like(arr)
        else:
            arr_device = dest

        arr_device.set_async(arr, stream=self._stream)
        self._synchronize()

        if dest is None:
            return arr_device

    def from_device(self, arr, dest=None, async=False):
        if async:
            arr_cpu = arr.get_async(ary=dest, stream=self._stream)
        else:
            arr_cpu = arr.get(ary=dest)

        if dest is None:
            return arr_cpu

    def copy_array(self, arr, dest=None, src_offset=0, dest_offset=0, size=None):

        if dest is None:
            arr_device = self.empty_like(arr)
        else:
            arr_device = dest

        itemsize = arr.dtype.itemsize
        nbytes = arr.nbytes if size is None else itemsize * size
        src_offset *= itemsize
        dest_offset *= itemsize

        cuda.memcpy_dtod_async(int(arr_device.gpudata) + dest_offset,
            int(arr.gpudata) + src_offset,
            nbytes, stream=self._stream)
        self._synchronize()

        if dest is None:
            return arr_device

    def synchronize(self):
        self._stream.synchronize()

    def _synchronize(self):
        if not self._async:
            self.synchronize()

    def _compile(self, src):
        options = ['-use_fast_math'] if self._fast_math else []
        try:
            module = SourceModule(src, no_extern_c=True, options=options)
        except:
            listing = "\n".join([str(i+1) + ":" + l for i, l in enumerate(src.split('\n'))])
            error("Failed to compile:\n" + listing)
            raise
        return module

    def compile(self, template_src, render_kwds=None):
        return Module(self, template_src, render_kwds=render_kwds)

    def compile_static(self, template_src, name, global_size,
            local_size=None, local_mem=0, render_kwds=None):
        return StaticKernel(self, template_src, name, global_size,
            local_size=local_size, render_kwds=render_kwds)

    def release(self):
        if not self._released:
            self._context.detach()
            self._released = True

    def __del__(self):
        self.release()


class DeviceParameters:

    def __init__(self, device):

        self.max_work_group_size = device.max_threads_per_block
        self.max_work_item_sizes = [
            device.max_block_dim_x,
            device.max_block_dim_y,
            device.max_block_dim_z]

        self.max_num_groups = [
            device.max_grid_dim_x,
            device.max_grid_dim_y,
            device.max_grid_dim_z]

        # there is no corresponding constant in the API at the moment
        self.local_mem_banks = 16 if device.compute_capability()[0] < 2 else 32

        self.warp_size = device.warp_size

        devdata = DeviceData(device)
        self.min_mem_coalesce_width = {
            size:devdata.align_words(word_size=size) for size in [4, 8, 16]}
        self.local_mem_size = device.max_shared_memory_per_block


class Module:

    def __init__(self, ctx, src, render_kwds=None):
        self._ctx = ctx

        if render_kwds is None:
            render_kwds = {}
        prelude = render_prelude(self._ctx)
        src = render_template_source(src, **render_kwds)

        self.source = prelude + src
        self._module = ctx._compile(self.source)

    def __getattr__(self, name):
        return Kernel(self._ctx, self._module.get_function(name))


class Kernel:

    def __init__(self, ctx, kernel):
        self._ctx = ctx
        self._kernel = kernel
        self._max_work_group_size = kernel.get_attribute(
            cuda.function_attribute.MAX_THREADS_PER_BLOCK)

    def prepare(self, global_size, local_size=None, local_mem=0):
        self.global_size = wrap_in_tuple(global_size)
        self.local_mem = local_mem

        if local_size is not None:
            self.local_size = wrap_in_tuple(local_size)
            if len(self.local_size) != len(self.global_size):
                raise ValueError("Global/local work sizes have differing dimensions")
        else:
            # Dumb algorithm of finding suitable local_size.
            # Works more or less the same as its OpenCL equivalent.
            max_size = self._max_work_group_size
            max_dims = self._ctx.device_params.max_work_item_sizes

            def fits_into_dims(block_size):
                """Checks if block dimensions fit into limits"""
                for md, bs in zip(max_dims, block_size):
                    if md < bs:
                        return False
                return True

            local_size_dims = [zip(*factors(g, limit=max_size))[0] for g in self.global_size]
            local_sizes = [t for t in itertools.product(*local_size_dims)
                if product(t) <= max_size and fits_into_dims(t)]
            self.local_size = max(local_sizes, key=product)

        # append missing dimensions, otherwise PyCUDA will complain
        self.local_size = self.local_size + (1,) * (3 - len(self.local_size))

        grid = []
        for gs, ls in zip(self.global_size, self.local_size):
            if gs % ls != 0:
                raise ValueError("Global sizes must be multiples of corresponding local sizes")
            grid.append(gs // ls)

        # append missing dimensions, otherwise PyCUDA will complain
        self.grid = tuple(grid) + (1,) * (3 - len(grid))

    def prepared_call(self, *args):
        self._kernel(*args, grid=self.grid, block=self.local_size,
            stream=self._ctx._stream, shared=self.local_mem)
        self._ctx._synchronize()

    def __call__(self, *args, **kwds):
        if 'global_size' in kwds:
            prep_args = (kwds.pop('global_size'),)
        else:
            prep_args = tuple()
        self.prepare(*prep_args, **kwds)
        self.prepared_call(*args)


class StaticKernel:

    def __init__(self, ctx, src, name, global_size, local_size=None, render_kwds=None):
        self._ctx = ctx

        if render_kwds is None:
            render_kwds = {}

        prelude = render_prelude(self._ctx)
        stub_vsize_funcs = render_stub_vsize_funcs()
        src = render_template_source(src, **render_kwds)

        # We need the first approximation of the maximum thread number for a kernel.
        # Stub virtual size functions instead of real ones will not change it (hopefully).
        stub_module = ctx._compile(str(prelude + stub_vsize_funcs + src))
        stub_kernel = stub_module.get_function(name)
        max_work_group_size = stub_kernel.get_attribute(
            cuda.function_attribute.MAX_THREADS_PER_BLOCK)

        vs = VirtualSizes(ctx.device_params, max_work_group_size, global_size, local_size)
        static_prelude = vs.render_vsize_funcs()
        self._global_size, self._local_size = vs.get_call_sizes()
        self._grid = tuple(g // l for g, l in zip(self._global_size, self._local_size))

        self.source = prelude + static_prelude + src
        self._module = ctx._compile(self.source)

        self._kernel = self._module.get_function(name)

        self.max_work_group_size = self._kernel.get_attribute(
            cuda.function_attribute.MAX_THREADS_PER_BLOCK)
        if self.max_work_group_size < product(self._local_size):
            raise cluda.OutOfResourcesError("Not enough registers/local memory for this local size")

    def __call__(self, *args):
        self._kernel(*args, grid=self._grid, block=self._local_size, stream=self._ctx._stream)
        self._ctx._synchronize()
