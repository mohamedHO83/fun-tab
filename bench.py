"""Micro-benchmark for the wheel render path (no windows are shown)."""

from __future__ import annotations

import statistics
import time

from fun_tab.config import Config
from fun_tab.overlay import Overlay
from fun_tab.preview import _demo_apps


def timeit(label: str, fn, runs: int = 30) -> float:
    fn()  # warm
    samples = []
    for _ in range(runs):
        start = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - start) * 1000)
    median = statistics.median(samples)
    print(f"{label:<44} {median:7.2f} ms   (min {min(samples):.2f})")
    return median


def main() -> None:
    for count in (5, 10, 20):
        cfg = Config()
        apps = _demo_apps(7)
        while len(apps) < count:
            apps = apps + _demo_apps(7)
        apps = apps[:count]
        for index, app in enumerate(apps):
            app.hwnd = index + 1

        overlay = Overlay(cfg)
        overlay._apps = apps
        overlay._all_apps = apps
        overlay._selected = 0
        overlay._apply_metrics(96)

        print(f"\n--- {count} windows ---")

        def cold_build():
            for cache in (
                overlay._layer_cache,
                overlay._icon_cache,
                overlay._icon_variants,
                overlay._plate_cache,
                overlay._wedge_cache,
                overlay._hub_cache,
                overlay._label_cache,
            ):
                cache.clear()
            overlay._rebuild_layers()
            overlay._compose(time.perf_counter())

        timeit("cold open, nothing cached at all", cold_build, runs=10)

        # First open after the startup prewarm: the prewarm itself is setup, so
        # it is deliberately outside the timed section.
        samples = []
        for _ in range(10):
            for cache in (
                overlay._layer_cache,
                overlay._icon_variants,
                overlay._plate_cache,
                overlay._wedge_cache,
                overlay._hub_cache,
                overlay._label_cache,
            ):
                cache.clear()
            overlay.prewarm_render(apps)
            overlay._apps, overlay._selected = apps, 0
            start = time.perf_counter()
            overlay._rebuild_layers()
            overlay._compose(time.perf_counter())
            samples.append((time.perf_counter() - start) * 1000)
        print(
            f"{'first open after startup prewarm':<44} "
            f"{statistics.median(samples):7.2f} ms   (min {min(samples):.2f})"
        )

        overlay._rebuild_layers()
        for i in range(count):
            overlay._selected = i
            overlay._compose(time.perf_counter())

        step = {"i": 0}

        def warm_frame():
            step["i"] = (step["i"] + 1) % count
            overlay._selected = step["i"]
            overlay._sel_from = -1
            overlay._compose(time.perf_counter())

        timeit("warm frame (cached layers, selection move)", warm_frame, runs=200)

        def crossfade_frame():
            overlay._selected = 1
            overlay._sel_from = 0
            overlay._sel_t0 = time.perf_counter() - 0.05
            overlay._compose(time.perf_counter())

        timeit("crossfade frame (two layers, alpha scaled)", crossfade_frame, runs=200)

        def reopen():
            overlay._rebuild_layers()  # same signature -> pure cache hit
            overlay._compose(time.perf_counter())

        timeit("re-open, unchanged window set", reopen, runs=200)

        def reorder():
            overlay._layer_cache.clear()  # MRU shuffled the wheel
            overlay._rebuild_layers()
            overlay._compose(time.perf_counter())

        timeit("re-open, window order changed", reorder, runs=100)


if __name__ == "__main__":
    main()
