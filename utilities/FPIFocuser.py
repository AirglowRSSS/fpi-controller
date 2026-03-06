#!/usr/bin/env python3
"""
FPIFocuser.py — Live focusing tool for the Fabry-Perot interferometer.

Monitors a directory for new HDF5 files, processes each image through the
FPI analysis pipeline (ReadIMG → FindCenter → FindEqualAreas → AnnularSum),
and maintains a live three-panel plot so the operator can assess focus quality
in real time.

Usage:
    python FPIFocuser.py <watch_dir> [options]
"""

import sys
import os
import argparse
import threading
import queue
import time
import traceback
from datetime import datetime

# ── Dependency check ──────────────────────────────────────────────────────────
_missing = []
try:
    import numpy as np
except ImportError:
    _missing.append('numpy')

try:
    import matplotlib
    matplotlib.use('Qt5Agg')
    import matplotlib.pyplot as plt
    import matplotlib.gridspec as gridspec
    import matplotlib.dates as mdates
    from matplotlib.colors import LinearSegmentedColormap
except ImportError:
    _missing.append('matplotlib')

try:
    import h5py  # noqa: F401  (needed by ReadIMG; imported here for startup check)
except ImportError:
    _missing.append('h5py')

if _missing:
    print(f"ERROR: Missing required dependencies: {', '.join(_missing)}", file=sys.stderr)
    print(f"Install with:  pip install {' '.join(_missing)}", file=sys.stderr)
    sys.exit(1)

_has_watchdog = True
try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
except ImportError:
    _has_watchdog = False

# ── FPI pipeline imports ───────────────────────────────────────────────────────
try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from FPI import ReadIMG, FindCenter, FindEqualAreas, AnnularSum
except ImportError as exc:
    print(f"ERROR: Could not import FPI functions: {exc}", file=sys.stderr)
    sys.exit(1)

# ── Sherwood colormap (same as DisplayRawMovie) ───────────────────────────────
_SHERWOOD_COLORS = [
    (0, 0, 0),
    (0, 0, 1),
    (0.95, 0, 0.63),
    (1, 0, 0),
    (1, 1, 0),
    (1, 1, 1),
]
SHERWOOD_CMAP = LinearSegmentedColormap.from_list('sherwood', _SHERWOOD_COLORS, N=512)


# ── Watchdog file-system handler ───────────────────────────────────────────────
class _HDF5Handler(FileSystemEventHandler):
    """Queue new .hdf5 / .h5 files as they appear."""

    def __init__(self, file_queue):
        super().__init__()
        self._q = file_queue

    def on_created(self, event):
        if event.is_directory:
            return
        if event.src_path.endswith('.hdf5') or event.src_path.endswith('.h5'):
            self._q.put(event.src_path)


# ── Polling fallback ───────────────────────────────────────────────────────────
def _poll_directory(watch_dir, file_queue, stop_event, poll_interval=2.0):
    """Simple polling fallback when watchdog is unavailable."""
    try:
        seen = set(os.listdir(watch_dir))
    except OSError:
        seen = set()

    while not stop_event.is_set():
        time.sleep(poll_interval)
        try:
            current = set(os.listdir(watch_dir))
        except OSError:
            continue
        for fname in sorted(current - seen):
            if fname.endswith('.hdf5') or fname.endswith('.h5'):
                file_queue.put(os.path.join(watch_dir, fname))
        seen = current


# ── FPI processing pipeline ───────────────────────────────────────────────────
def _safe_read(filepath, max_retries=3, retry_delay=0.5):
    """
    Call ReadIMG with retries to handle files that are still being written.

    Returns (d, img) where d is the ReadIMG result and img is a 2-D numpy array.
    Raises the last exception if all retries fail.
    """
    last_exc = None
    for attempt in range(max_retries):
        try:
            d = ReadIMG(filepath)
            img = np.asarray(d)
            if img is None or img.ndim != 2 or img.size == 0:
                raise ValueError(f"ReadIMG returned unusable array (shape={getattr(img,'shape','?')})")
            return d, img
        except Exception as exc:
            last_exc = exc
            if attempt < max_retries - 1:
                time.sleep(retry_delay)
    raise last_exc


def process_file(filepath, N, peak_min, peak_max, user_center):
    """
    Run the full FPI pipeline on one HDF5 file.

    Parameters
    ----------
    filepath    : str
    N           : int  — number of equal-area annuli
    peak_min    : int  — start index of peak-search window
    peak_max    : int  — end index (exclusive) of peak-search window
    user_center : (float, float) or None

    Returns
    -------
    timestamp    : datetime
    peak_value   : float
    peak_idx     : int   — absolute annulus index of the detected peak
    spectra      : ndarray, shape (N,)
    annuli       : dict  — from FindEqualAreas
    img          : ndarray, 2-D
    cx, cy       : float — center used
    center_source: 'user' or 'auto'
    """
    d, img = _safe_read(filepath)

    # Timestamp — prefer HDF5 metadata, fall back to file mtime
    try:
        timestamp = d.info['LocalTime']
    except (AttributeError, KeyError):
        timestamp = datetime.fromtimestamp(os.path.getmtime(filepath))

    # Center determination
    if user_center is not None:
        cx, cy = user_center
        center_source = 'user'
    else:
        cx, cy = FindCenter(img)
        center_source = 'auto'

    # Annular decomposition
    annuli = FindEqualAreas(img, cx, cy, N)
    spectra, _sigma = AnnularSum(img, annuli, 0)  # sigma discarded

    # Peak within search window
    window = spectra[peak_min:peak_max]
    peak_value = float(np.max(window))
    peak_idx = int(np.argmax(window)) + peak_min

    return timestamp, peak_value, peak_idx, spectra, annuli, img, cx, cy, center_source


# ── Figure construction ────────────────────────────────────────────────────────
def build_figure():
    """
    Create the three-panel figure.

    Layout
    ------
    [ Left: ring image (spans 2 rows) | Right top: peak history  ]
    [                                 | Right bottom: annular sum ]

    Returns (fig, ax_image, ax_history, ax_annular).
    """
    fig = plt.figure(figsize=(14, 6))
    gs = gridspec.GridSpec(
        2, 2,
        width_ratios=[1.4, 1.0],
        height_ratios=[1, 1],
        hspace=0.50,
        wspace=0.38,
    )
    ax_image   = fig.add_subplot(gs[:, 0])   # left column, both rows
    ax_history = fig.add_subplot(gs[0, 1])   # right top
    ax_annular = fig.add_subplot(gs[1, 1])   # right bottom

    ax_image.set_aspect('equal')
    ax_image.set_title('Waiting for first image…', fontsize=9)
    ax_image.axis('off')

    ax_history.set_title('Peak value history', fontsize=9)
    ax_history.set_ylabel('Peak intensity', fontsize=8)

    ax_annular.set_title('Annular sum — current image', fontsize=9)
    ax_annular.set_xlabel('Radius index', fontsize=8)
    ax_annular.set_ylabel('Intensity', fontsize=8)

    fig.tight_layout()
    return fig, ax_image, ax_history, ax_annular


# ── Live plot update ───────────────────────────────────────────────────────────
def update_plots(
    fig, ax_image, ax_history, ax_annular,
    filepath, timestamp, peak_value, peak_idx,
    spectra, annuli, img, cx, cy, center_source,
    peak_min, peak_max,
    history_times, history_peaks,
):
    """Redraw all three panels with the current image's data."""

    fname  = os.path.basename(filepath)
    ts_str = timestamp.strftime('%H:%M:%S')

    # ── Left panel: ring image ────────────────────────────────────────────────
    ax_image.cla()
    ax_image.set_aspect('equal')
    ax_image.axis('off')

    vmin = float(np.quantile(img, 0.2))
    vmax = float(np.quantile(img, 0.8))
    ax_image.imshow(img, cmap=SHERWOOD_CMAP, vmin=vmin, vmax=vmax, origin='upper')

    # Center marker — style depends on source
    if center_source == 'user':
        ax_image.plot(cx, cy, 'rx', markersize=12, markeredgewidth=2.5,
                      zorder=5)
    else:
        ax_image.plot(cx, cy, 'w+', markersize=10, markeredgewidth=1.5,
                      zorder=5)

    # Annotation callout in bottom-left corner
    source_label = '(user)' if center_source == 'user' else '(auto)'
    ax_image.text(
        0.02, 0.03,
        f'Center: ({cx:.1f}, {cy:.1f})  {source_label}',
        transform=ax_image.transAxes,
        fontsize=8,
        color='white',
        va='bottom',
        bbox=dict(boxstyle='round,pad=0.3', facecolor='black', alpha=0.55),
    )

    ax_image.set_title(f'{fname}\n{ts_str}', fontsize=9)

    # ── Right top: peak value history ─────────────────────────────────────────
    ax_history.cla()
    ax_history.set_title('Peak value history', fontsize=9)
    ax_history.set_ylabel('Peak intensity', fontsize=8)

    if history_times:
        ax_history.plot(history_times, history_peaks,
                        'o-', color='steelblue', markersize=4, linewidth=1.2)

        session_max = max(history_peaks)
        ax_history.axhline(
            session_max,
            color='tomato', linestyle='--', linewidth=1.0,
            label=f'Session max: {session_max:.1f}',
        )
        ax_history.legend(fontsize=8, loc='upper left')

        # Format x-axis as HH:MM
        ax_history.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
        plt.setp(ax_history.xaxis.get_majorticklabels(),
                 rotation=45, ha='right', fontsize=7)

        ax_history.relim()
        ax_history.autoscale_view()

    # ── Right bottom: annular sum ─────────────────────────────────────────────
    ax_annular.cla()
    ax_annular.set_title('Annular sum — current image', fontsize=9)
    ax_annular.set_xlabel('Radius index', fontsize=8)
    ax_annular.set_ylabel('Intensity', fontsize=8)

    radii = np.arange(len(spectra))
    ax_annular.plot(radii, spectra, color='steelblue', linewidth=1.2)

    # Shade the peak-search window
    ax_annular.axvspan(
        peak_min, peak_max,
        alpha=0.15, color='orange', zorder=0,
        label=f'Search [{peak_min}–{peak_max}]',
    )

    # Mark detected peak
    ax_annular.axvline(
        peak_idx,
        color='tomato', linestyle='--', linewidth=1.0,
        label=f'Peak @ {peak_idx}  ({peak_value:.1f})',
    )

    ax_annular.legend(fontsize=7, loc='upper right')
    ax_annular.relim()
    ax_annular.autoscale_view()

    fig.canvas.draw_idle()
    plt.pause(0.05)


# ── Entry point ───────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description='FPIFocuser — live FPI focusing aid',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        'watch_dir',
        help='Directory to monitor for new HDF5 files',
    )
    parser.add_argument(
        '--cadence', type=float, default=30, metavar='SECONDS',
        help='Expected image cadence in seconds (display hint only)',
    )
    parser.add_argument(
        '--peak-min', type=int, default=20, dest='peak_min', metavar='INT',
        help='Minimum radius index for peak search',
    )
    parser.add_argument(
        '--peak-max', type=int, default=100, dest='peak_max', metavar='INT',
        help='Maximum radius index for peak search',
    )
    parser.add_argument(
        '--N', type=int, default=500, dest='N', metavar='INT',
        help='Number of equal-area annuli passed to FindEqualAreas',
    )
    parser.add_argument(
        '--cx', type=float, default=None, metavar='FLOAT',
        help='X coordinate of center in pixels (must be paired with --cy)',
    )
    parser.add_argument(
        '--cy', type=float, default=None, metavar='FLOAT',
        help='Y coordinate of center in pixels (must be paired with --cx)',
    )
    args = parser.parse_args()

    # Validate paired --cx / --cy
    if (args.cx is None) != (args.cy is None):
        parser.error('--cx and --cy must be specified together')

    # Validate peak window fits within N
    if args.peak_min < 0 or args.peak_max > args.N or args.peak_min >= args.peak_max:
        parser.error(
            f'--peak-min ({args.peak_min}) and --peak-max ({args.peak_max}) '
            f'must satisfy 0 <= peak_min < peak_max <= N ({args.N})'
        )

    watch_dir   = os.path.abspath(args.watch_dir)
    user_center = (args.cx, args.cy) if args.cx is not None else None

    if not os.path.isdir(watch_dir):
        parser.error(f'watch_dir does not exist: {watch_dir}')

    # ── Startup message ───────────────────────────────────────────────────────
    print('=' * 60)
    print('FPIFocuser — live FPI focusing tool')
    print(f'  Watch directory : {watch_dir}')
    print(f'  Cadence hint    : {args.cadence} s')
    print(f'  Annuli (N)      : {args.N}')
    print(f'  Peak search     : indices {args.peak_min} – {args.peak_max}')
    if user_center is not None:
        print(f'  Center          : user-specified ({args.cx:.2f}, {args.cy:.2f})')
    else:
        print('  Center          : auto (FindCenter per image)')
    if not _has_watchdog:
        print('  WARNING: watchdog not found — using 2-second polling fallback')
    print('=' * 60)
    print('Press Ctrl-C or close the plot window to exit.\n')

    # ── Shared state ──────────────────────────────────────────────────────────
    file_queue   = queue.Queue()
    shutdown_flag = threading.Event()

    # ── Start directory monitor ───────────────────────────────────────────────
    observer = None
    if _has_watchdog:
        handler  = _HDF5Handler(file_queue)
        observer = Observer()
        observer.schedule(handler, watch_dir, recursive=False)
        observer.start()
        print('Monitoring with watchdog observer.')
    else:
        poll_thread = threading.Thread(
            target=_poll_directory,
            args=(watch_dir, file_queue, shutdown_flag),
            daemon=True,
        )
        poll_thread.start()
        print('Monitoring with 2-second polling.')

    # ── Build figure ──────────────────────────────────────────────────────────
    plt.ion()
    fig, ax_image, ax_history, ax_annular = build_figure()
    plt.show(block=False)
    plt.pause(0.1)

    def _on_close(_event):
        shutdown_flag.set()

    fig.canvas.mpl_connect('close_event', _on_close)

    # Session history (parallel lists)
    history_times = []   # list of datetime
    history_peaks = []   # list of float

    # ── Main loop ─────────────────────────────────────────────────────────────
    try:
        while not shutdown_flag.is_set():
            # Block briefly so the GUI event loop can run
            try:
                filepath = file_queue.get(timeout=0.2)
            except queue.Empty:
                plt.pause(0.05)
                continue

            print(f'Processing: {os.path.basename(filepath)}')
            try:
                (timestamp, peak_value, peak_idx,
                 spectra, annuli, img,
                 cx, cy, center_source) = process_file(
                    filepath, args.N, args.peak_min, args.peak_max, user_center,
                )
            except Exception:
                print(
                    f'ERROR: failed to process {os.path.basename(filepath)}',
                    file=sys.stderr,
                )
                traceback.print_exc(file=sys.stderr)
                continue

            history_times.append(timestamp)
            history_peaks.append(peak_value)

            print(f'  Time    : {timestamp.strftime("%Y-%m-%d %H:%M:%S")}')
            print(f'  Center  : ({cx:.1f}, {cy:.1f})  [{center_source}]')
            print(f'  Peak    : {peak_value:.1f}  at index {peak_idx}')

            update_plots(
                fig, ax_image, ax_history, ax_annular,
                filepath, timestamp, peak_value, peak_idx,
                spectra, annuli, img, cx, cy, center_source,
                args.peak_min, args.peak_max,
                history_times, history_peaks,
            )

    except KeyboardInterrupt:
        print('\nCtrl-C received — shutting down…')
    finally:
        shutdown_flag.set()
        if observer is not None:
            observer.stop()
            observer.join()
        print('FPIFocuser stopped.')


if __name__ == '__main__':
    main()
