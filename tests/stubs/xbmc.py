"""Náhrada modulu `xbmc` pro testy mimo Kodi. Nic nedělá, jen si pamatuje, co po ní kdo chtěl."""
import json

LOGDEBUG, LOGINFO, LOGWARNING, LOGERROR, LOGFATAL = 0, 1, 2, 3, 4
ISO_639_1, ISO_639_2, ENGLISH_NAME = 0, 1, 2

logged = []          # (zpráva, úroveň)
builtins = []        # executebuiltin
rpc_calls = []       # executeJSONRPC (rozparsované)
info_labels = {}     # getInfoLabel → hodnota
cond_visible = set()  # getCondVisibility → True pro tyhle podmínky
abort = False         # Monitor.abortRequested() — test nastaví True = „Kodi končí"


def log(msg, level=LOGDEBUG):
    logged.append((msg, level))


def executebuiltin(cmd, wait=False):
    builtins.append(cmd)


def executeJSONRPC(payload):
    rpc_calls.append(json.loads(payload))
    return json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}})


def getInfoLabel(label):
    return info_labels.get(label, "")


def getCondVisibility(cond):
    return cond in cond_visible


def getLanguage(fmt=ENGLISH_NAME, region=False):
    return "cs" if fmt == ISO_639_1 else "Czech"


def sleep(ms):
    pass


class Monitor:
    def abortRequested(self):
        return abort

    def waitForAbort(self, timeout=0):
        # v testech nikdy nečekat — smyčky se ukončí, jako by Kodi končilo
        return True


class Player:
    def isPlaying(self):
        return False

    def isPlayingVideo(self):
        return False

    def getPlayingFile(self):
        return ""

    def getTime(self):
        return 0.0

    def getTotalTime(self):
        return 0.0


class _Detail:
    def __init__(self, *args, **kwargs):
        self.args, self.kwargs = args, kwargs


class AudioStreamDetail(_Detail):
    pass


class VideoStreamDetail(_Detail):
    pass


class SubtitleStreamDetail(_Detail):
    pass


class Actor(_Detail):
    pass


def reset():
    global abort
    del logged[:], builtins[:], rpc_calls[:]
    info_labels.clear()
    cond_visible.clear()
    abort = False
