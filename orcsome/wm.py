import os
import fcntl
import logging

from select import select

from . import xlib as X
from .wrappers import Window

logger = logging.getLogger(__name__)

MODIFICATORS = {
  'Alt': X.Mod1Mask,
  'Control': X.ControlMask,
  'Ctrl': X.ControlMask,
  'Shift': X.ShiftMask,
  'Win': X.Mod4Mask,
  'Mod': X.Mod4Mask,
}

IGNORED_MOD_MASKS = (0, X.LockMask, X.Mod2Mask, X.LockMask | X.Mod2Mask)


class RestartException(Exception): pass


class WM(object):
    """Core orcsome instance

    Can be get in any time as::

        import orcsome
        wm = orcsome.get_wm()
    """

    def __init__(self):
        self.rfifo, self.wfifo = os.pipe()
        fl = fcntl.fcntl(self.rfifo, fcntl.F_GETFL)
        fcntl.fcntl(self.rfifo, fcntl.F_SETFL, fl | os.O_NONBLOCK)

        self.key_handlers = {}
        self.property_handlers = {}
        self.create_handlers = []
        self.destroy_handlers = {}
        self.init_handlers = []
        self.deinit_handlers = []
        self.signal_handlers = {}

        self.grab_keyboard_handler = None
        self.grab_pointer_handler = None

        self.focus_history = []

        self.dpy = X.XOpenDisplay(X.NULL)
        if self.dpy == X.NULL:
            raise Exception("Can't open display")

        self.fd = X.ConnectionNumber(self.dpy)
        self.root = X.DefaultRootWindow(self.dpy)
        self.atom = X.AtomCache(self.dpy)

        self.undecorated_atom_name = '_OB_WM_STATE_UNDECORATED'

    def window(self, window_id):
        window = Window(window_id)
        window.wm = self
        return window

    def emit(self, signal):
        os.write(self.wfifo, signal + '\n')

    def keycode(self, key):
        sym = X.XStringToKeysym(key)
        if sym is X.NoSymbol:
            logger.error('Invalid key [%s]' % key)
            return None

        return X.XKeysymToKeycode(self.dpy, sym)

    def bind_key(self, window, keydef):
        parts = keydef.split('+')
        mod, key = parts[:-1], parts[-1]
        modmask = 0
        for m in mod:
            try:
                modmask |= MODIFICATORS[m]
            except KeyError:
                logger.error('Invalid key [%s]' % keydef)
                return lambda func: func

        sym = X.XStringToKeysym(key)
        if sym is X.NoSymbol:
            logger.error('Invalid key [%s]' % keydef)
            return lambda func: func

        code = X.XKeysymToKeycode(self.dpy, sym)

        def inner(func):
            keys = []
            for imask in IGNORED_MOD_MASKS:
                mask = modmask | imask
                X.XGrabKey(self.dpy, code, mask, window, True, X.GrabModeAsync, X.GrabModeAsync)
                self.key_handlers.setdefault(window, {})[(mask, code)] = func
                keys.append((mask, code))

            def remove():
                for k in keys:
                    del self.key_handlers[window][k]

            func.remove = remove
            return func

        return inner

    def on_key(self, *args):
        """Signal decorator to define hotkey

        You can define global key::

           wm.on_key('Alt+Return')(
               spawn('xterm'))

        Or key binded to specific window::

           @wm.on_create(cls='URxvt')
           def bind_urxvt_keys():
               # Custom key to close only urxvt windows
               wm.on_key(wm.event_window, 'Ctrl+d')(
                   close)

        Key defenition is a string in format ``[mod + ... +]keysym`` where ``mod`` is
        one of modificators [Alt, Shift, Control(Ctrl), Mod(Win)] and
        ``keysym`` is a key name.
        """

        if isinstance(args[0], long):
            window = args[0]
            key = args[1]
        else:
            window = self.root
            key = args[0]

        return self.bind_key(window, key)

    def _on_create_manage(self, ignore_startup, *args, **matchers):
        """Signal decorator to handle window creation

        Can be used in two forms. Listen to any window creation::

           @wm.on_create
           def debug(wm):
               print wm.event_window.get_wm_class()

        Or specific window::

           @wm.on_create(cls='Opera')
           def use_firefox_luke(wm):
               wm.close_window(wm.event_window)
               spawn('firefox')()

        Also, orcsome calls on_create handlers on its startup.
        You can check ``wm.startup`` attribute to denote such event.

        See :meth:`is_match` for ``**matchers`` argument description.
        """
        def inner(func):
            if matchers:
                ofunc = func
                func = lambda: self.event_window.matches(**matchers) and ofunc()

            if ignore_startup:
                oofunc = func
                func = lambda: self.startup or oofunc()

            self.create_handlers.append(func)

            def remove():
                self.create_handlers.remove(func)

            func.remove = remove
            return func

        if args:
            return inner(args[0])
        else:
            return inner

    def on_create(self, *args, **matchers):
        return self._on_create_manage(True, *args, **matchers)

    def on_manage(self, *args, **matchers):
        return self._on_create_manage(False, *args, **matchers)

    def on_destroy(self, window):
        """Signal decorator to handle window destroy"""

        def inner(func):
            self.destroy_handlers.setdefault(window, []).append(func)
            return func

        return inner

    def on_property_change(self, *args):
        """Signal decorator to handle window property change

        One can handle any window property change::

           @wm.on_property_change('_NET_WM_STATE')
           def window_maximized_state_change():
               state = wm.get_window_state(wm.event_window)
               if state.maximized_vert and state.maximized_horz:
                   print 'Look, ma! Window is maximized now!'

        And specific window::

           @wm.on_create
           def switch_to_desktop():
               if not wm.startup:
                   if wm.activate_window_desktop(wm.event_window) is None:
                       # Created window has no any attached desktop so wait for it
                       @wm.on_property_change(wm.event_window, '_NET_WM_DESKTOP')
                       def property_was_set():
                           wm.activate_window_desktop(wm.event_window)
                           property_was_set.remove()

        """
        def inner(func):
            if isinstance(args[0], long):
                window = args[0]
                props = args[1:]
            else:
                window = None
                props = args

            for p in props:
                atom = self.atom[p]
                self.property_handlers.setdefault(
                    atom, {}).setdefault(window, []).append(func)

            def remove():
                for p in props:
                    atom = self.atom[p]
                    self.property_handlers[atom][window].remove(func)

            func.remove = remove
            return func

        return inner

    def get_clients(self, ids=False):
        """Return wm client list"""
        result = X.get_window_property(self.dpy, self.root,
            self.atom['_NET_CLIENT_LIST'], self.atom['WINDOW']) or []

        if not ids:
            result = [self.window(r) for r in result]

        return result

    def get_stacked_clients(self):
        """Return client list in stacked order.

        Most top window will be last in list. Can be useful to determine window visibility.
        """
        return X.get_window_property(self.dpy, self.root,
            self.atom['_NET_CLIENT_LIST_STACKING'], self.atom['WINDOW']) or []

    @property
    def current_window(self):
        """Return currently active (with input focus) window"""
        result = X.get_window_property(self.dpy, self.root,
            self.atom['_NET_ACTIVE_WINDOW'], self.atom['WINDOW'])

        if result:
            return self.window(result[0])

    @property
    def current_desktop(self):
        """Return current desktop number

        Counts from zero.
        """
        return X.get_window_property(self.dpy, self.root,
            self.atom['_NET_CURRENT_DESKTOP'])[0]

    def activate_desktop(self, num):
        """Activate desktop ``num``"""
        if num < 0:
            return

        self._send_event(self.root, self.atom['_NET_CURRENT_DESKTOP'], [num])
        self._flush()

    def _send_event(self, window, mtype, data):
        data = (data + ([0] * (5 - len(data))))[:5]
        ev = X.ffi.new('XClientMessageEvent *', {
            'type': X.ClientMessage,
            'window': window,
            'message_type': mtype,
            'format': 32,
            'data': {'l': data},
        })
        X.XSendEvent(self.dpy, self.root, False, X.SubstructureRedirectMask,
            X.ffi.cast('XEvent *', ev))

    def _flush(self):
        X.XFlush(self.dpy)

    def find_clients(self, clients, **matchers):
        """Return matching clients list

        :param clients: window list returned by :meth:`get_clients` or :meth:`get_stacked_clients`.
        :param \*\*matchers: keyword arguments defined in :meth:`is_match`
        """
        return [r for r in clients if r.matches(**matchers)]

    def find_client(self, clients, **matchers):
        """Return first matching client

        :param clients: window list returned by :meth:`get_clients` or :meth:`get_stacked_clients`.
        :param \*\*matchers: keyword arguments defined in :meth:`is_match`
        """
        result = self.find_clients(clients, **matchers)
        try:
            return result[0]
        except IndexError:
            return None

    def process_create_window(self, window):
        X.XSelectInput(self.dpy, window, X.StructureNotifyMask |
            X.PropertyChangeMask | X.FocusChangeMask)

        self.event_window = window
        for handler in self.create_handlers:
            handler()

    def init(self):
        X.XSelectInput(self.dpy, self.root, X.SubstructureNotifyMask)

        for h in self.init_handlers:
            h()

        self.startup = True
        for c in self.get_clients():
            self.process_create_window(c)

        X.XSync(self.dpy, False)

        X.XSetErrorHandler(error_handler)

    def handle_keypress(self, event):
        event = event.xkey
        if self.grab_keyboard_handler:
            self.grab_keyboard_handler(True, event.state, event.keycode)
        else:
            try:
                handler = self.key_handlers[event.window][(event.state, event.keycode)]
            except KeyError:
                pass
            else:
                self.event = event
                self.event_window = self.window(event.window)
                handler()

    def handle_keyrelease(self, event):
        event = event.xkey
        if self.grab_keyboard_handler:
            self.grab_keyboard_handler(False, event.state, event.keycode)

    def handle_create(self, event):
        event = event.xcreatewindow
        self.event = event
        self.startup = False
        self.process_create_window(self.window(event.window))

    def handle_destroy(self, event):
        event = event.xdestroywindow
        try:
            handlers = self.destroy_handlers[event.window]
        except KeyError:
            pass
        else:
            self.event = event
            self.event_window = self.window(event.window)
            for h in handlers:
                h()
        finally:
            self._clean_window_data(event.window)

    def handle_property(self, event):
        event = event.xproperty
        atom = event.atom
        if event.state == 0 and atom in self.property_handlers:
            wphandlers = self.property_handlers[atom]
            self.event_window = self.window(event.window)
            self.event = event
            if event.window in wphandlers:
                for h in wphandlers[event.window]:
                    h()

            if None in wphandlers:
                for h in wphandlers[None]:
                    h()

    def handle_focusin(self, event):
        event = event.xfocus
        try:
            self.focus_history.remove(event.window)
        except ValueError:
            pass

        self.focus_history.append(event.window)

    def run(self):
        handlers = {
            X.KeyPress: self.handle_keypress,
            X.KeyRelease: self.handle_keyrelease,
            X.CreateNotify: self.handle_create,
            X.DestroyNotify: self.handle_destroy,
            X.FocusIn: self.handle_focusin,
            X.PropertyNotify: self.handle_property,
        }

        event = X.create_event()

        while True:
            try:
                readable, _, _ = select([self.fd, self.rfifo], [], [])
            except KeyboardInterrupt:
                return True

            if not readable:
                continue

            if self.fd in readable:
                while True:
                    try:
                        i = X.XPending(self.dpy)
                    except KeyboardInterrupt:
                        return True

                    if not i:
                        break

                    while i > 0:
                        X.XNextEvent(self.dpy, event)
                        i = i - 1

                        try:
                            h = handlers[event.type]
                        except KeyError:
                            continue

                        try:
                            h(event)
                        except (KeyboardInterrupt, SystemExit):
                            return True
                        except RestartException:
                            return False
                        except:
                            logger.exception('Boo')

            if self.rfifo in readable:
                for s in os.read(self.rfifo, 8192).splitlines():
                    if s in self.signal_handlers:
                        for h in self.signal_handlers[s]:
                            try:
                                h()
                            except (KeyboardInterrupt, SystemExit):
                                return True
                            except RestartException:
                                return False
                            except:
                                logger.exception('Boo')


    def _clean_window_data(self, window):
        if window in self.key_handlers:
            del self.key_handlers[window]

        if window in self.destroy_handlers:
            self.destroy_handlers[window]

        try:
            self.focus_history.remove(window)
        except ValueError:
            pass

        for atom, whandlers in self.property_handlers.items():
            if window in whandlers:
                del whandlers[window]

            if not self.property_handlers[atom]:
                del self.property_handlers[atom]

    def focus_window(self, window):
        """Activate window"""
        self._send_event(window, self.atom['_NET_ACTIVE_WINDOW'], [2, X.CurrentTime])
        self._flush()

    def focus_and_raise(self, window):
        """Activate window desktop, set input focus and raise it"""
        self.activate_window_desktop(window)
        X.XConfigureWindow(self.dpy, window, X.CWStackMode,
            X.ffi.new('XWindowChanges *', {'stack_mode': X.Above}))
        self.focus_window(window)

    def place_window_above(self, window):
        """Float up window in wm stack"""
        X.XConfigureWindow(self.dpy, window, X.CWStackMode,
            X.ffi.new('XWindowChanges *', {'stack_mode': X.Above}))
        self._flush()

    def place_window_below(self, window):
        """Float down window in wm stack"""
        X.XConfigureWindow(self.dpy, window, X.CWStackMode,
            X.ffi.new('XWindowChanges *', {'stack_mode': X.Below}))
        self._flush()

    def activate_window_desktop(self, window):
        """Activate window desktop

        Return:

        * True if window is placed on different from current desktop
        * False if window desktop is the same
        * None if window does not have desktop property
        """
        wd = window.desktop
        if wd is not None:
            if self.current_desktop != wd:
                self.activate_desktop(wd)
                return True
            else:
                return False
        else:
            return None

    def set_window_state(self, window, taskbar=None, pager=None,
            decorate=None, otaskbar=None, vmax=None, hmax=None):
        """Set window state"""
        state_atom = self.atom['_NET_WM_STATE']

        if decorate is not None:
            params = not decorate, self.atom[self.undecorated_atom_name]
            self._send_event(window, state_atom, list(params))

        if taskbar is not None:
            params = not taskbar, self.atom['_NET_WM_STATE_SKIP_TASKBAR']
            self._send_event(window, state_atom, list(params))

        if vmax is not None and vmax == hmax:
            params = vmax, self.atom['_NET_WM_STATE_MAXIMIZED_VERT'], \
                self.atom['_NET_WM_STATE_MAXIMIZED_HORZ']
            self._send_event(window, state_atom, list(params))

        if otaskbar is not None:
            params = [] if otaskbar else [self.atom['_ORCSOME_SKIP_TASKBAR']]
            X.set_window_property(self.dpy, window, self.atom['_ORCSOME_STATE'],
                self.atom['ATOM'], 32, params)

        if pager is not None:
            params = not pager, self.atom['_NET_WM_STATE_SKIP_PAGER']
            self._send_event(window, state_atom, list(params))

        self._flush()

    def close_window(self, window):
        """Send request to wm to close window"""
        self._send_event(window, self.atom['_NET_CLOSE_WINDOW'], [X.CurrentTime])
        self._flush()

    def change_window_desktop(self, window, desktop):
        """Move window to ``desktop``"""
        if desktop < 0:
            return

        self._send_event(window, self.atom['_NET_WM_DESKTOP'], [desktop])
        self._flush()

    # def set_window_desktop(self, window, desktop):
    #     if desktop < 0:
    #         desktop = 0xffffffff

    #     X.set_window_property(self.dpy, window, self.atom['_NET_WM_DESKTOP'],
    #         self.atom['CARDINAL'], 32, [desktop])
    #     self._flush()

    def stop(self, is_exit=False):
        self.key_handlers.clear()
        self.property_handlers.clear()
        self.create_handlers[:] = []
        self.destroy_handlers.clear()
        self.focus_history[:] = []

        self.signal_handlers.clear()

        if not is_exit:
            X.XUngrabKey(self.dpy, X.AnyKey, X.AnyModifier, self.root)
            for window in self.get_clients():
                X.XUngrabKey(self.dpy, X.AnyKey, X.AnyModifier, window)

        for h in self.deinit_handlers:
            try:
                h()
            except:
                logger.exception('Shutdown error')

        self.init_handlers[:] = []
        self.deinit_handlers[:] = []

    def grab_keyboard(self, func):
        if self.grab_keyboard_handler:
            return False

        result = X.XGrabKeyboard(self.dpy, self.root, False, X.GrabModeAsync,
            X.GrabModeAsync, X.CurrentTime)

        if result == 0:
            self.grab_keyboard_handler = func
            return True

        return False

    def ungrab_keyboard(self):
        self.grab_keyboard_handler = None
        return X.XUngrabKeyboard(self.dpy, X.CurrentTime)

    def grab_pointer(self, func):
        if self.grab_pointer_handler:
            return False

        result = X.XGrabPointer(self.dpy, self.root, False, 0,
            X.GrabModeAsync, X.GrabModeAsync, X.NONE, X.NONE, X.CurrentTime)

        if result == 0:
            self.grab_pointer_handler = func
            return True

        return False

    def ungrab_pointer(self):
        self.grab_pointer_handler = None
        return X.XUngrabPointer(self.dpy, X.CurrentTime)

    def on_init(self, func):
        self.init_handlers.append(func)
        return func

    def on_deinit(self, func):
        self.deinit_handlers.append(func)
        return func

    def on_signal(self, signal):
        def inner(func):
            self.signal_handlers.setdefault(signal, []).append(func)

            def remove():
                self.signal_handlers[signal].remove(func)

            func.remove = remove
            return func

        return inner

    def get_screen_saver_info(self):
        result = X.ffi.new('XScreenSaverInfo *')
        X.XScreenSaverQueryInfo(self.dpy, self.root, result)
        return result


@X.ffi.callback('XErrorHandler')
def error_handler(display, error):
    msg = X.ffi.new('char[100]')
    X.XGetErrorText(display, error.error_code, msg, 100)
    logger.error('{} ({}:{})'.format(X.ffi.string(msg), error.request_code, error.minor_code))
    return 0