"""Design a pair of first-order allpass chains whose outputs are 90 degrees apart
over the bass range at 48 kHz. Prints C arrays for prism_bass.c.

Section: H(z) = (-c + z^-1) / (1 - c z^-1), pole at z = c (0 < c < 1, a low
frequency), c = (1 - t) / (1 + t), t = tan(pi f / fs). Each chain gets N poles;
their frequencies are optimised (log scale) for the smallest worst-case error.
"""
import numpy as np
from scipy.optimize import minimize

SR = 48000
F_LO, F_HI = 12.0, 1500.0
N = 5
w = 2 * np.pi * np.geomspace(F_LO, F_HI, 600) / SR
z1 = np.exp(-1j * w)


def coefs(logf):
    t = np.tan(np.pi * np.exp(logf) / SR)
    return (1 - t) / (1 + t)


def phase(cs):
    h = np.ones_like(z1)
    for c in cs:
        h *= (-c + z1) / (1 - c * z1)
    return np.unwrap(np.angle(h))


def deviation(x):
    d = phase(coefs(x[:N])) - phase(coefs(x[N:]))
    return np.degrees(np.abs(np.abs(d) - np.pi / 2))


def cost(x, p=24):
    return np.mean(deviation(x) ** p) ** (1 / p)


# Interleave pole frequencies over a slightly wider band than needed.
start = np.log(np.geomspace(F_LO / 3, F_HI * 3, 2 * N))
x0 = np.concatenate([start[0::2], start[1::2]])
# Poles must stay at valid, stable frequencies: 1 Hz .. 20 kHz (fs/2 = 24 kHz).
# Optimise in an unbounded variable mapped onto that range, starting from the
# known-good layout found without bounds (whose top pole fell above fs/2).
LO, HI = np.log(1.0), np.log(20000.0)


def to_logf(y):
    return LO + (HI - LO) / (1 + np.exp(-y))


def to_y(logf):
    u = np.clip((logf - LO) / (HI - LO), 1e-6, 1 - 1e-6)
    return np.log(u / (1 - u))


good = np.log(np.array([1.0, 4.23, 32.7, 134.38, 551.99, 14.79, 66.86, 270.06, 1218.15, 19000.0]))
y = to_y(good)
for p in (24, 48, 96, 160):
    r = minimize(lambda v: cost(to_logf(v), p), y, method="Nelder-Mead",
                 options={"maxiter": 80000, "xatol": 1e-11, "fatol": 1e-13, "adaptive": True})
    y = r.x


class Best:
    pass


best = Best()
best.x = to_logf(y)
dev = deviation(best.x)
print(f"max deviation from 90 deg: {dev.max():.3f} deg over {F_LO}-{F_HI} Hz")
print("pole Hz A:", np.round(np.sort(np.exp(best.x[:N])), 2), " B:", np.round(np.sort(np.exp(best.x[N:])), 2))
ca, cb = coefs(best.x[:N]), coefs(best.x[N:])
assert np.all(np.abs(ca) < 1) and np.all(np.abs(cb) < 1), "unstable section"
print("static const double HILBERT_A[] = {" + ", ".join(f"{c:.12f}" for c in coefs(best.x[:N])) + "};")
print("static const double HILBERT_B[] = {" + ", ".join(f"{c:.12f}" for c in coefs(best.x[N:])) + "};")
sign = np.sign(np.median(phase(coefs(best.x[:N])) - phase(coefs(best.x[N:]))))
print(f"/* chain A leads chain B by {'+' if sign > 0 else '-'}90 degrees */")
