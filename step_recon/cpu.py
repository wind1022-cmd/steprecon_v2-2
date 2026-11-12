"""How much of the computer the engine may use: the number of processor cores and the priority.

The geometry kernel (OpenCascade) runs its heavy steps on worker threads spread over every core, so an analysis can
fill the whole CPU. Limiting the cores this process may run on (its affinity) caps that for all its threads, the
kernel's included, and can be changed while work is running. A low priority lets other programs go first.

    apply(cores=4, low=True)     state() -> {"total", "cores", "low", "can_limit", "can_raise"}
    parse("50%") -> cores        default_cores()

Windows: affinity + priority class. Linux: affinity for every thread + nice (a raised niceness cannot be lowered
again without privileges). Elsewhere only the priority."""
import os

_low = False
_cores = None


def _count():
    return os.cpu_count() or 1


if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _k = ctypes.WinDLL("kernel32", use_last_error=True)
    _k.GetCurrentProcess.restype = wintypes.HANDLE
    _k.GetProcessAffinityMask.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_size_t), ctypes.POINTER(ctypes.c_size_t)]
    _k.SetProcessAffinityMask.argtypes = [wintypes.HANDLE, ctypes.c_size_t]
    _k.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _k.GetPriorityClass.argtypes = [wintypes.HANDLE]
    _k.GetPriorityClass.restype = wintypes.DWORD
    _BELOW_NORMAL, _NORMAL = 0x4000, 0x20

    def _masks():
        proc, system = ctypes.c_size_t(), ctypes.c_size_t()
        if not _k.GetProcessAffinityMask(_k.GetCurrentProcess(), ctypes.byref(proc), ctypes.byref(system)):
            return 0, 0
        return proc.value, system.value

    _SYSTEM = [b for b in range(64) if _masks()[1] >> b & 1]     # the cores of this process's processor group

    def _set_cores(n):
        mask = 0
        for b in _SYSTEM[:n]:
            mask |= 1 << b
        return bool(_k.SetProcessAffinityMask(_k.GetCurrentProcess(), mask))

    def _get_cores():
        return bin(_masks()[0]).count("1") or len(_SYSTEM)

    def _set_low(low):
        return bool(_k.SetPriorityClass(_k.GetCurrentProcess(), _BELOW_NORMAL if low else _NORMAL))

    def _get_low():
        return _k.GetPriorityClass(_k.GetCurrentProcess()) in (_BELOW_NORMAL, 0x40)      # below normal / idle

    CAN_LIMIT, CAN_RAISE = bool(_SYSTEM), True
    TOTAL = len(_SYSTEM) or _count()

elif hasattr(os, "sched_setaffinity"):
    _SYSTEM = sorted(os.sched_getaffinity(0))

    def _set_cores(n):
        want = set(_SYSTEM[:n])
        ok = True
        for tid in os.listdir("/proc/self/task") if os.path.isdir("/proc/self/task") else ["0"]:
            try:        # on Linux the affinity belongs to each thread: set it for the running ones too
                os.sched_setaffinity(int(tid), want)
            except OSError:
                ok = False
        return ok

    def _get_cores():
        return len(os.sched_getaffinity(0))

    def _set_low(low):
        try:
            now = os.getpriority(os.PRIO_PROCESS, 0)
            if low and now < 10:
                os.setpriority(os.PRIO_PROCESS, 0, 10)
            elif not low and now > 0:
                os.setpriority(os.PRIO_PROCESS, 0, 0)       # works only with privileges
            return True
        except OSError:
            return False

    def _get_low():
        try:
            return os.getpriority(os.PRIO_PROCESS, 0) > 0
        except OSError:
            return False

    CAN_LIMIT, CAN_RAISE = True, False
    TOTAL = len(_SYSTEM)

else:
    def _set_cores(n):
        return False

    def _get_cores():
        return _count()

    def _set_low(low):
        try:
            if low:
                os.nice(10)
            return True
        except OSError:
            return False

    def _get_low():
        return _low

    CAN_LIMIT, CAN_RAISE = False, False
    TOTAL = _count()


def default_cores():
    """Half of the cores: an analysis stays reasonably quick and the computer stays usable."""
    return max(1, TOTAL // 2)


def parse(text):
    """'4' -> 4 cores, '50%' -> half of the cores, 'all' -> every core. Raises ValueError."""
    t = str(text).strip().lower()
    if t in ("all", "max", "전체"):
        return TOTAL
    if t.endswith("%"):
        n = round(TOTAL * float(t[:-1]) / 100.0)
    else:
        n = int(float(t))
    if n < 1:
        raise ValueError(f"at least 1 core is needed (got {text!r})")
    return min(n, TOTAL)


def apply(cores=None, low=None):
    """Set the number of cores (1 .. TOTAL) and/or low priority. Returns state()."""
    global _low, _cores
    if cores is not None:
        n = max(1, min(int(cores), TOTAL))
        if CAN_LIMIT and _set_cores(n):
            _cores = n
    if low is not None:
        if _set_low(bool(low)):
            _low = bool(low)
    return state()


def state():
    return {"total": TOTAL, "cores": _get_cores() if CAN_LIMIT else TOTAL, "low": bool(_get_low()),
            "can_limit": CAN_LIMIT, "can_raise": CAN_RAISE}
