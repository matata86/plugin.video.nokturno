"""Náhrada `xbmcgui` — položky, dialogy a okna jen zaznamenávají volání.

Dialogy vracejí „nic nevybráno“, aby žádná cesta v testu nečekala na uživatele.
"""
NOTIFICATION_INFO, NOTIFICATION_WARNING, NOTIFICATION_ERROR = "info", "warning", "error"
INPUT_ALPHANUM, INPUT_NUMERIC, INPUT_PASSWORD = 0, 1, 2
ALPHANUM_HIDE_INPUT = 2

notifications = []   # (nadpis, zpráva, druh)
textviewers = []     # (nadpis, text)
oks = []             # (nadpis, zpráva) — Dialog().ok()
_window_props = {}


class _Recorder:
    """Objekt, který si pamatuje každé volání metody a nikdy nespadne."""

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def call(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return None
        return call


class InfoTagVideo(_Recorder):
    pass


class ListItem:
    def __init__(self, label="", label2="", path="", offscreen=False):
        self.label, self.label2, self.path = label, label2, path
        self.subtitles = []
        self.art, self.properties, self.info = {}, {}, {}
        self.context = []
        self.tag = InfoTagVideo()
        self.calls = []

    def setSubtitles(self, paths):
        self.subtitles = list(paths)

    def getLabel(self):
        return self.label

    def setLabel(self, label):
        self.label = label

    def setArt(self, art):
        self.art.update(art or {})

    def setInfo(self, kind, info):
        self.info.update(info or {})

    def setProperty(self, key, value):
        self.properties[key] = value

    def getProperty(self, key):
        return self.properties.get(key, "")

    def addContextMenuItems(self, items, replaceItems=False):
        # jako Kodi: každé volání přepíše položky od začátku
        self.context = list(items)

    def getVideoInfoTag(self):
        return self.tag

    def __getattr__(self, name):
        def call(*args, **kwargs):
            self.calls.append((name, args, kwargs))
        return call


class Dialog:
    def notification(self, heading, message, icon=NOTIFICATION_INFO, time=5000, sound=True):
        notifications.append((heading, message, icon))

    def textviewer(self, heading, text, usemono=False):
        textviewers.append((heading, text))

    def ok(self, heading, message):
        oks.append((heading, message))
        return True

    def yesno(self, *args, **kwargs):
        return False

    def yesnocustom(self, *args, **kwargs):
        return -1

    def select(self, *args, **kwargs):
        return -1

    def multiselect(self, *args, **kwargs):
        return None

    def input(self, *args, **kwargs):
        return ""

    def browse(self, *args, **kwargs):
        return ""


class DialogProgress(_Recorder):
    def iscanceled(self):
        return False


class DialogProgressBG(_Recorder):
    def isFinished(self):
        return False


class Window:
    def __init__(self, window_id=None):
        self.id = window_id

    def getProperty(self, key):
        return _window_props.get(key, "")

    def setProperty(self, key, value):
        _window_props[key] = value

    def clearProperty(self, key):
        _window_props.pop(key, None)


def reset():
    del notifications[:], textviewers[:], oks[:]
    _window_props.clear()


class _Control:
    def __init__(self, *args, **kwargs):
        self.args, self.kwargs, self.text = args, kwargs, ""

    def setText(self, text):
        self.text = text

    def getId(self):
        return id(self)


class ControlImage(_Control):
    pass


class ControlLabel(_Control):
    pass


class ControlButton(_Control):
    pass


class ControlTextBox(_Control):
    pass


windows_shown = []


class WindowDialog:
    def __init__(self):
        self.controls = []
        self.focused = None

    def addControl(self, control):
        self.controls.append(control)

    def setFocus(self, control):
        self.focused = control

    def getFocusId(self):
        if self.focused is None:
            raise RuntimeError("No control has focus")
        return self.focused.getId()

    def show(self):
        windows_shown.append(self)

    def close(self):
        pass
