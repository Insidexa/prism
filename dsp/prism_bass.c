/*
 * Prism Bass - level-independent psychoacoustic bass (LADSPA).
 *
 * Small speakers cannot reproduce the lowest notes, but the ear infers a bass
 * note from its harmonics. This plugin isolates the bass band and adds its
 * 2nd, 3rd and 4th harmonics at fixed levels relative to the fundamental. The
 * harmonics are computed exactly (cos k*theta from the band's analytic
 * signal), so their level does not depend on how loud the music is - unlike
 * saturation-based bass enhancers, which do nothing at low levels and too
 * much at high ones. The input passes through unchanged; harmonics are added.
 *
 * Signal flow per channel:
 *   x -> high-pass -> low-pass -> b
 *   b -> allpass chain A -> I, allpass chain B -> Q   (I, Q 90 deg apart, 12 Hz-1.5 kHz)
 *   e = |I + jQ|,  u = (I + jQ) / e = e^{j theta}
 *   y = x + amount * e * (g2 cos 2theta + g3 cos 3theta + g4 cos 4theta)
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 */
#include <ladspa.h>
#include <math.h>
#include <stdlib.h>
#include <string.h>

/* 90-degree allpass pair for 48 kHz, designed by dsp/design_hilbert.py:
 * max deviation 0.42 deg over 12-1500 Hz. At other sample rates the design is
 * recomputed from the same pole frequencies (see pole_hz below). */
static const double POLE_HZ_A[] = {1.0, 3.5, 40.28, 197.62, 1045.32};
static const double POLE_HZ_B[] = {16.8, 89.75, 439.63, 4032.32, 20000.0};
#define NSEC 5

enum {
    P_IN_L, P_IN_R, P_OUT_L, P_OUT_R,
    P_H2, P_H3, P_H4, P_LOWPASS, P_HIGHPASS, P_AMOUNT, P_ENABLE,
    P_COUNT
};

typedef struct {
    double b0, b1, b2, a1, a2;
    double z1, z2;
} Biquad;

typedef struct {
    double c[NSEC];
    double x1[NSEC], y1[NSEC];
} Allpass;

typedef struct {
    Biquad hp, lp;
    Allpass a, b;
} Channel;

typedef struct {
    LADSPA_Data *port[P_COUNT];
    double rate;
    double cur_lp, cur_hp;
    Channel ch[2];
} Plugin;

static void biquad_set(Biquad *q, int lowpass, double f, double rate)
{
    /* RBJ cookbook, Q = 1/sqrt(2) (Butterworth). */
    double w = 2.0 * M_PI * f / rate, cw = cos(w), sw = sin(w);
    double alpha = sw / (2.0 * M_SQRT1_2);
    double a0 = 1.0 + alpha;
    if (lowpass) {
        q->b0 = (1.0 - cw) / 2.0 / a0;
        q->b1 = (1.0 - cw) / a0;
        q->b2 = q->b0;
    } else {
        q->b0 = (1.0 + cw) / 2.0 / a0;
        q->b1 = -(1.0 + cw) / a0;
        q->b2 = q->b0;
    }
    q->a1 = -2.0 * cw / a0;
    q->a2 = (1.0 - alpha) / a0;
}

static inline double biquad_run(Biquad *q, double x)
{
    /* Transposed direct form II. */
    double y = q->b0 * x + q->z1;
    q->z1 = q->b1 * x - q->a1 * y + q->z2;
    q->z2 = q->b2 * x - q->a2 * y;
    return y;
}

static void allpass_set(Allpass *ap, const double *pole_hz, double rate)
{
    for (int i = 0; i < NSEC; i++) {
        double f = pole_hz[i] < 0.45 * rate ? pole_hz[i] : 0.45 * rate;
        double t = tan(M_PI * f / rate);
        ap->c[i] = (1.0 - t) / (1.0 + t);
        ap->x1[i] = ap->y1[i] = 0.0;
    }
}

static inline double allpass_run(Allpass *ap, double x)
{
    /* H(z) = (-c + z^-1) / (1 - c z^-1) per section. */
    for (int i = 0; i < NSEC; i++) {
        double y = -ap->c[i] * x + ap->x1[i] + ap->c[i] * ap->y1[i];
        ap->x1[i] = x;
        ap->y1[i] = y;
        x = y;
    }
    return x;
}

static double db(double v)
{
    return pow(10.0, v / 20.0);
}

static LADSPA_Handle instantiate(const LADSPA_Descriptor *d, unsigned long rate)
{
    (void)d;
    Plugin *p = calloc(1, sizeof(Plugin));
    if (!p)
        return NULL;
    p->rate = (double)rate;
    return p;
}

static void connect_port(LADSPA_Handle h, unsigned long port, LADSPA_Data *data)
{
    ((Plugin *)h)->port[port] = data;
}

static void configure_filters(Plugin *p, double lp, double hp)
{
    for (int c = 0; c < 2; c++) {
        biquad_set(&p->ch[c].hp, 0, hp, p->rate);
        biquad_set(&p->ch[c].lp, 1, lp, p->rate);
    }
    p->cur_lp = lp;
    p->cur_hp = hp;
}

static void activate(LADSPA_Handle h)
{
    Plugin *p = h;
    memset(p->ch, 0, sizeof(p->ch));
    for (int c = 0; c < 2; c++) {
        allpass_set(&p->ch[c].a, POLE_HZ_A, p->rate);
        allpass_set(&p->ch[c].b, POLE_HZ_B, p->rate);
    }
    configure_filters(p, *p->port[P_LOWPASS], *p->port[P_HIGHPASS]);
}

static void run(LADSPA_Handle h, unsigned long n)
{
    Plugin *p = h;
    double lp = *p->port[P_LOWPASS], hp = *p->port[P_HIGHPASS];
    if (lp != p->cur_lp || hp != p->cur_hp)
        configure_filters(p, lp, hp);

    const int enabled = *p->port[P_ENABLE] > 0.5f;
    const double amount = db(*p->port[P_AMOUNT]);
    const double g2 = db(*p->port[P_H2]) * amount;
    const double g3 = db(*p->port[P_H3]) * amount;
    const double g4 = db(*p->port[P_H4]) * amount;

    for (int c = 0; c < 2; c++) {
        const LADSPA_Data *in = p->port[c == 0 ? P_IN_L : P_IN_R];
        LADSPA_Data *out = p->port[c == 0 ? P_OUT_L : P_OUT_R];
        Channel *ch = &p->ch[c];
        for (unsigned long i = 0; i < n; i++) {
            double x = in[i];
            double b = biquad_run(&ch->lp, biquad_run(&ch->hp, x));
            double I = allpass_run(&ch->a, b);
            double Q = allpass_run(&ch->b, b);
            double e = sqrt(I * I + Q * Q);
            double harm = 0.0;
            if (e > 1e-9) {
                double co = I / e, si = Q / e;
                double c2 = co * co - si * si, s2 = 2.0 * co * si;
                double c3 = c2 * co - s2 * si;
                double c4 = c2 * c2 - s2 * s2;
                harm = e * (g2 * c2 + g3 * c3 + g4 * c4);
            }
            out[i] = (LADSPA_Data)(enabled ? x + harm : x);
        }
        /* Keep filter states out of the denormal range during silence. */
        for (int s = 0; s < NSEC; s++) {
            if (fabs(ch->a.y1[s]) < 1e-30) ch->a.y1[s] = 0.0;
            if (fabs(ch->b.y1[s]) < 1e-30) ch->b.y1[s] = 0.0;
        }
    }
}

static void cleanup(LADSPA_Handle h)
{
    free(h);
}

static const char *const port_names[P_COUNT] = {
    "In L", "In R", "Out L", "Out R",
    "2nd harmonic (dB)", "3rd harmonic (dB)", "4th harmonic (dB)",
    "Bass band top (Hz)", "Bass band bottom (Hz)", "Amount (dB)", "Enable",
};

static const LADSPA_PortDescriptor port_desc[P_COUNT] = {
    LADSPA_PORT_INPUT | LADSPA_PORT_AUDIO, LADSPA_PORT_INPUT | LADSPA_PORT_AUDIO,
    LADSPA_PORT_OUTPUT | LADSPA_PORT_AUDIO, LADSPA_PORT_OUTPUT | LADSPA_PORT_AUDIO,
    LADSPA_PORT_INPUT | LADSPA_PORT_CONTROL, LADSPA_PORT_INPUT | LADSPA_PORT_CONTROL,
    LADSPA_PORT_INPUT | LADSPA_PORT_CONTROL, LADSPA_PORT_INPUT | LADSPA_PORT_CONTROL,
    LADSPA_PORT_INPUT | LADSPA_PORT_CONTROL, LADSPA_PORT_INPUT | LADSPA_PORT_CONTROL,
    LADSPA_PORT_INPUT | LADSPA_PORT_CONTROL,
};

#define BOUNDED (LADSPA_HINT_BOUNDED_BELOW | LADSPA_HINT_BOUNDED_ABOVE)
static const LADSPA_PortRangeHint port_hints[P_COUNT] = {
    {0, 0, 0}, {0, 0, 0}, {0, 0, 0}, {0, 0, 0},
    {BOUNDED | LADSPA_HINT_DEFAULT_MIDDLE, -60.0f, 0.0f},     /* 2nd: -30 dB */
    {BOUNDED | LADSPA_HINT_DEFAULT_LOW, -60.0f, 0.0f},        /* 3rd: -45 dB */
    {BOUNDED | LADSPA_HINT_DEFAULT_LOW, -60.0f, 0.0f},        /* 4th: -45 dB */
    {BOUNDED | LADSPA_HINT_DEFAULT_MIDDLE, 60.0f, 400.0f},    /* 230 Hz */
    {BOUNDED | LADSPA_HINT_DEFAULT_MINIMUM, 10.0f, 80.0f},    /* 10 Hz */
    {BOUNDED | LADSPA_HINT_DEFAULT_0, -24.0f, 12.0f},
    {LADSPA_HINT_TOGGLED | LADSPA_HINT_DEFAULT_1, 0.0f, 1.0f},
};

static const LADSPA_Descriptor descriptor = {
    .UniqueID = 0x50524d42, /* 'PRMB' */
    .Label = "prism_bass",
    .Properties = LADSPA_PROPERTY_HARD_RT_CAPABLE,
    .Name = "Prism Bass (level-independent psychoacoustic bass)",
    .Maker = "Prism",
    .Copyright = "GPL-3.0-or-later",
    .PortCount = P_COUNT,
    .PortDescriptors = port_desc,
    .PortNames = port_names,
    .PortRangeHints = port_hints,
    .instantiate = instantiate,
    .connect_port = connect_port,
    .activate = activate,
    .run = run,
    .cleanup = cleanup,
};

/* ------------------------------------------------------------------------
 * Prism Limiter - stereo-linked look-ahead peak limiter.
 *
 * The audio is delayed by the look-ahead time; the gain needed for every
 * incoming sample is known before that sample reaches the output, so the gain
 * can fall smoothly ahead of a peak. The required gain is the minimum over the
 * look-ahead window (sliding-window minimum), attacked over that window and
 * released exponentially. A final clamp guarantees the ceiling.
 * ------------------------------------------------------------------------ */
enum {
    L_IN_L, L_IN_R, L_OUT_L, L_OUT_R,
    L_THRESHOLD, L_RELEASE, L_GR,
    L_COUNT
};

#define LA_MS 5.0
#define LA_MAX 4096

typedef struct {
    LADSPA_Data *port[L_COUNT];
    double rate;
    int la;                      /* look-ahead in samples */
    float dl[2][LA_MAX];         /* delay lines */
    double req[LA_MAX];          /* required gain history (ring) */
    int pos;
    /* monotonic deque of absolute sample numbers for the sliding minimum */
    long dq[LA_MAX];
    int head, tail;
    long n;                      /* sample counter */
    double gain;
} Limiter;

static LADSPA_Handle lim_instantiate(const LADSPA_Descriptor *d, unsigned long rate)
{
    (void)d;
    Limiter *l = calloc(1, sizeof(Limiter));
    if (!l)
        return NULL;
    l->rate = (double)rate;
    l->la = (int)(LA_MS * 1e-3 * rate);
    if (l->la < 1) l->la = 1;
    if (l->la > LA_MAX - 2) l->la = LA_MAX - 2;
    return l;
}

static void lim_connect(LADSPA_Handle h, unsigned long port, LADSPA_Data *data)
{
    ((Limiter *)h)->port[port] = data;
}

static void lim_activate(LADSPA_Handle h)
{
    Limiter *l = h;
    memset(l->dl, 0, sizeof(l->dl));
    for (int i = 0; i < LA_MAX; i++) l->req[i] = 1.0;
    l->pos = 0;
    l->head = l->tail = 0;
    l->n = 0;
    l->gain = 1.0;
}

static void lim_run(LADSPA_Handle h, unsigned long n)
{
    Limiter *l = h;
    const double thr = db(*l->port[L_THRESHOLD]);
    double rel_ms = *l->port[L_RELEASE];
    if (rel_ms < 1.0) rel_ms = 1.0;
    const double rel = exp(-1.0 / (rel_ms * 1e-3 * l->rate));
    const double att = exp(-1.0 / (l->la / 3.0));  /* settles within the look-ahead */
    const int W = l->la + 1;
    double min_g = 1.0;

    for (unsigned long i = 0; i < n; i++) {
        double xl = l->port[L_IN_L][i], xr = l->port[L_IN_R][i];
        double peak = fmax(fabs(xl), fabs(xr));
        double r = peak > thr ? thr / peak : 1.0;

        /* sliding minimum of r over the last W samples (monotonic deque) */
        l->req[l->n % LA_MAX] = r;
        while (l->tail != l->head &&
               l->req[l->dq[(l->tail - 1 + LA_MAX) % LA_MAX] % LA_MAX] >= r)
            l->tail = (l->tail - 1 + LA_MAX) % LA_MAX;
        l->dq[l->tail] = l->n;
        l->tail = (l->tail + 1) % LA_MAX;
        while (l->dq[l->head] <= l->n - W)
            l->head = (l->head + 1) % LA_MAX;
        double target = l->req[l->dq[l->head] % LA_MAX];
        l->n++;

        /* smooth: fast towards less gain, slow back up */
        l->gain = target < l->gain ? target + (l->gain - target) * att
                                   : target + (l->gain - target) * rel;
        if (l->gain < min_g) min_g = l->gain;

        /* delayed audio out */
        int d = l->pos;
        float ol = l->dl[0][d], orr = l->dl[1][d];
        l->dl[0][d] = (float)xl;
        l->dl[1][d] = (float)xr;
        l->pos = (l->pos + 1) % l->la;
        double yl = ol * l->gain, yr = orr * l->gain;
        if (yl > thr) yl = thr; else if (yl < -thr) yl = -thr;
        if (yr > thr) yr = thr; else if (yr < -thr) yr = -thr;
        l->port[L_OUT_L][i] = (LADSPA_Data)yl;
        l->port[L_OUT_R][i] = (LADSPA_Data)yr;
    }
    if (l->port[L_GR])
        *l->port[L_GR] = (LADSPA_Data)(20.0 * log10(min_g > 1e-9 ? min_g : 1e-9));
}

static const char *const lim_names[L_COUNT] = {
    "In L", "In R", "Out L", "Out R", "Threshold (dBFS)", "Release (ms)", "Gain reduction (dB)",
};
static const LADSPA_PortDescriptor lim_desc[L_COUNT] = {
    LADSPA_PORT_INPUT | LADSPA_PORT_AUDIO, LADSPA_PORT_INPUT | LADSPA_PORT_AUDIO,
    LADSPA_PORT_OUTPUT | LADSPA_PORT_AUDIO, LADSPA_PORT_OUTPUT | LADSPA_PORT_AUDIO,
    LADSPA_PORT_INPUT | LADSPA_PORT_CONTROL, LADSPA_PORT_INPUT | LADSPA_PORT_CONTROL,
    LADSPA_PORT_OUTPUT | LADSPA_PORT_CONTROL,
};
static const LADSPA_PortRangeHint lim_hints[L_COUNT] = {
    {0, 0, 0}, {0, 0, 0}, {0, 0, 0}, {0, 0, 0},
    {BOUNDED | LADSPA_HINT_DEFAULT_MAXIMUM, -24.0f, 0.0f},
    {BOUNDED | LADSPA_HINT_DEFAULT_LOW, 5.0f, 500.0f},
    {0, 0, 0},
};

static void lim_cleanup(LADSPA_Handle h)
{
    free(h);
}

static const LADSPA_Descriptor lim_descriptor = {
    .UniqueID = 0x50524d4c, /* 'PRML' */
    .Label = "prism_limiter",
    .Properties = LADSPA_PROPERTY_HARD_RT_CAPABLE,
    .Name = "Prism Limiter (stereo look-ahead peak limiter)",
    .Maker = "Prism",
    .Copyright = "GPL-3.0-or-later",
    .PortCount = L_COUNT,
    .PortDescriptors = lim_desc,
    .PortNames = lim_names,
    .PortRangeHints = lim_hints,
    .instantiate = lim_instantiate,
    .connect_port = lim_connect,
    .activate = lim_activate,
    .run = lim_run,
    .cleanup = lim_cleanup,
};

const LADSPA_Descriptor *ladspa_descriptor(unsigned long index)
{
    switch (index) {
    case 0: return &descriptor;
    case 1: return &lim_descriptor;
    default: return NULL;
    }
}
