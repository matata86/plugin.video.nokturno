"""Náhrada `xbmcplugin` — sbírá položky výpisu a to, jak plugin skončil."""
SORT_METHOD_NONE, SORT_METHOD_LABEL, SORT_METHOD_SIZE, SORT_METHOD_DATE = 0, 1, 2, 3
SORT_METHOD_VIDEO_YEAR, SORT_METHOD_VIDEO_RATING, SORT_METHOD_EPISODE = 4, 5, 6
SORT_METHOD_UNSORTED, SORT_METHOD_TITLE, SORT_METHOD_LABEL_IGNORE_THE = 7, 8, 9
SORT_METHOD_DURATION, SORT_METHOD_GENRE, SORT_METHOD_PLAYCOUNT = 10, 11, 12

items = []        # (handle, url, listitem, is_folder)
ended = []        # {"handle", "succeeded", "updateListing", "cacheToDisc"}
resolved = []     # (handle, succeeded, listitem)
contents = []     # setContent
sort_methods = []
categories = []


def addDirectoryItem(handle, url, listitem, isFolder=False, totalItems=0):
    items.append((handle, url, listitem, isFolder))
    return True


def addDirectoryItems(handle, rows, totalItems=0):
    for url, listitem, is_folder in rows:
        items.append((handle, url, listitem, is_folder))
    return True


def endOfDirectory(handle, succeeded=True, updateListing=False, cacheToDisc=True):
    ended.append({"handle": handle, "succeeded": succeeded, "updateListing": updateListing,
                  "cacheToDisc": cacheToDisc})


def setResolvedUrl(handle, succeeded, listitem):
    resolved.append((handle, succeeded, listitem))


def setContent(handle, content):
    contents.append(content)


def addSortMethod(handle, method, *args, **kwargs):
    sort_methods.append(method)


def setPluginCategory(handle, category):
    categories.append(category)


def setProperty(handle, key, value):
    pass


def reset():
    del items[:], ended[:], resolved[:], contents[:], sort_methods[:], categories[:]


def urls():
    return [url for _h, url, _li, _f in items]
