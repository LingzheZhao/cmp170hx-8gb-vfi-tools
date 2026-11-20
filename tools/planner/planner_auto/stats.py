"""Binomial bounds for lock-in hit rates."""
import math
from statistics import NormalDist


def z_one_sided(confidence):
    return NormalDist().inv_cdf(confidence)


def wilson_lower(k, n, confidence=0.95):
    """One-sided Wilson score lower bound for k successes in n trials (0 when n == 0)."""
    if n <= 0:
        return 0.0
    z = z_one_sided(confidence)
    p = k / n
    z2 = z * z
    centre = p + z2 / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n))
    return max(0.0, (centre - margin) / (1 + z2 / n))


def wilson_upper(k, n, confidence=0.95):
    if n <= 0:
        return 1.0
    z = z_one_sided(confidence)
    p = k / n
    z2 = z * z
    centre = p + z2 / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n))
    return min(1.0, (centre + margin) / (1 + z2 / n))


def runs_needed(target, confidence=0.95, max_n=10000):
    """Smallest n with n/n successes whose Wilson lower bound reaches target (a best case for planning)."""
    for n in range(1, max_n + 1):
        if wilson_lower(n, n, confidence) >= target:
            return n
    return None
