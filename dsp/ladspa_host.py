"""Run a LADSPA plugin over a WAV file offline (for tests).

Usage: python ladspa_host.py [--index=N] plugin.so in.wav out.wav [port=value ...]
"""
import ctypes as C
import sys
import wave

import numpy as np

LADSPA_Handle = C.c_void_p


class Descriptor(C.Structure):
    _fields_ = [("UniqueID", C.c_ulong), ("Label", C.c_char_p), ("Properties", C.c_int),
                ("Name", C.c_char_p), ("Maker", C.c_char_p), ("Copyright", C.c_char_p),
                ("PortCount", C.c_ulong), ("PortDescriptors", C.POINTER(C.c_int)),
                ("PortNames", C.POINTER(C.c_char_p)), ("PortRangeHints", C.c_void_p),
                ("ImplementationData", C.c_void_p),
                ("instantiate", C.CFUNCTYPE(LADSPA_Handle, C.c_void_p, C.c_ulong)),
                ("connect_port", C.CFUNCTYPE(None, LADSPA_Handle, C.c_ulong, C.POINTER(C.c_float))),
                ("activate", C.CFUNCTYPE(None, LADSPA_Handle)),
                ("run", C.CFUNCTYPE(None, LADSPA_Handle, C.c_ulong)),
                ("run_adding", C.c_void_p), ("set_run_adding_gain", C.c_void_p),
                ("deactivate", C.c_void_p),
                ("cleanup", C.CFUNCTYPE(None, LADSPA_Handle))]


def process(so, x, rate, controls, block=512, index=0):
    lib = C.CDLL(so)
    lib.ladspa_descriptor.restype = C.POINTER(Descriptor)
    d = lib.ladspa_descriptor(index).contents
    h = d.instantiate(C.byref(d), rate)
    names = [d.PortNames[i].decode() for i in range(d.PortCount)]
    ctrl = {}
    for i, name in enumerate(names):
        if d.PortDescriptors[i] & 4:  # control
            ctrl[i] = C.c_float(controls.get(name, 0.0))
            d.connect_port(h, i, C.pointer(ctrl[i]))
    inL, inR = np.zeros(block, np.float32), np.zeros(block, np.float32)
    outL, outR = np.zeros(block, np.float32), np.zeros(block, np.float32)
    for i, buf in enumerate((inL, inR, outL, outR)):
        d.connect_port(h, i, buf.ctypes.data_as(C.POINTER(C.c_float)))
    d.activate(h)
    y = np.zeros_like(x, dtype=np.float32)
    for s in range(0, len(x), block):
        n = min(block, len(x) - s)
        inL[:n], inR[:n] = x[s:s + n, 0], x[s:s + n, 1]
        d.run(h, n)
        y[s:s + n, 0], y[s:s + n, 1] = outL[:n], outR[:n]
    d.cleanup(h)
    return y


def read(path):
    with wave.open(path) as w:
        sw, ch, rate = w.getsampwidth(), w.getnchannels(), w.getframerate()
        raw = w.readframes(w.getnframes())
    x = np.frombuffer(raw, {2: "<i2", 4: "<i4"}[sw]).astype(np.float64) / (2 ** (8 * sw - 1))
    x = x.reshape(-1, ch)
    return (np.repeat(x, 2, 1) if ch == 1 else x), rate


def write(path, y, rate):
    with wave.open(path, "wb") as w:
        w.setnchannels(2); w.setsampwidth(4); w.setframerate(rate)
        w.writeframes((np.clip(y, -1, 1) * (2 ** 31 - 1)).astype("<i4").tobytes())


if __name__ == "__main__":
    args = sys.argv[1:]
    index = 0
    if args and args[0].startswith("--index="):
        index = int(args.pop(0).split("=")[1])
    so, src, dst = args[:3]
    controls = {}
    for kv in args[3:]:
        k, v = kv.rsplit("=", 1)
        controls[k] = float(v)
    x, rate = read(src)
    write(dst, process(so, x, rate, controls, index=index), rate)
