import re
from collections import namedtuple
import logging

from Xlib import X, Xatom
import Xlib.display
from Xlib.XK import string_to_keysym, load_keysym_group
from Xlib.protocol.event import ClientMessage

MODIFICATORS = {
  'Alt': X.Mod1Mask,
  'Control': X.ControlMask,
  'Ctrl': X.ControlMask,
  'Shift': X.ShiftMask,
  'Win': X.Mod4Mask,
  'Mod': X.Mod4Mask,
}

IGNORED_MOD_MASKS = (0, X.LockMask, X.Mod2Mask)

load_keysym_group('xf86')

WindowState = namedtuple('State', 'maximized_vert, maximized_horz, undecorated')

class RestartException(Exception): pass


class WM(object):
    def __init__(self):
        self.key_handlers = {}
        self.property_handlers = {}
        self.create_handlers = []
        self.destroy_handlers = {}

        self.focus_history = []

        self.re_cache = {}

        self.dpy = Xlib.display.Display()
        self.root = self.dpy.screen().root

    def bind_key(self, window, keydef):
        """Signal decorator to to define window's hotkey

        Can be useful with :meth:`on_create`::

           @on_create(cls='URxvt')
           def bind_urxvt_keys(wm):
               # Custom key to close only urxvt windows
               wm.bind_key(wm.event_window, 'Ctrl+d')(close)

        ``key`` is string in format ``[mod + ... +]keysym`` where ``mod`` is
        one of modificators [Alt, Shift, Control(Ctrl), Mod(Win)] and
        ``keysym`` is key name.
        """
        parts = keydef.split('+')
        mod, key = parts[:-1], parts[-1]
        modmask = 0
        for m in mod:
            try:
                modmask |= MODIFICATORS[m]
            except KeyError:
                logging.getLogger(__name__).error('Invalid key [%s]' % keydef)
                return lambda func: func

        sym = string_to_keysym(key)
        if sym is X.NoSymbol:
            logging.getLogger(__name__).error('Invalid key [%s]' % keydef)
            return lambda func: func

        code = self.dpy.keysym_to_keycode(sym)

        def inner(func):
            keys = []
            wid = window.id
            for imask in IGNORED_MOD_MASKS:
                mask = modmask | imask
                window.grab_key(code, mask, True, X.GrabModeAsync, X.GrabModeAsync)
                self.key_handlers.setdefault(window.id, {})[(mask, code)] = func
                keys.append((mask, code))

            def remove():
                for k in keys:
                    del self.key_handlers[wid][k]

            func.remove = remove
            return func

        return inner

    def on_key(self, *args):
        """Signal decorator to define global hotkey

        ::

           on_key('Alt+Return')(
               spawn('xterm'))

           # or

           @on_key('Alt+Return')
           def spawn_terminal(wm):
               spawn('xterm')(wm)

        :param key: see :meth:`bind_key`
        """

        if getattr(args[0], 'id', False):
            window = args[0]
            key = args[1]
        else:
            window = self.root
            key = args[0]

        return self.bind_key(window, key)

    def on_create(self, *args, **matchers):
        """Signal decorator to handle window creation

        Can be used in two forms. Listen to any window creation::

           @on_create
           def debug(wm):
               print wm.event_window.get_wm_class()

        And specific window::

           @on_create(cls='Opera')
           def use_firefox_luke(wm):
               wm.close_window(wm.event_window)

        Also, orcsome calls on_create handlers on startup.
        You can check ``wm.startup`` attribute do denote such event.

        See :meth:`is_match` for ``**matchers`` argument description.
        """

        if args:
            func = args[0]
            self.create_handlers.append(func)

            def remove():
                self.create_handlers.remove(func)

            func.remove = remove
            return func

        def inner(func):
            def match_window():
                if self.is_match(self.event_window, **matchers):
                    func()

            self.create_handlers.append(match_window)

            def remove():
                self.create_handlers.remove(match_window)

            func.remove = remove
            return func

        return inner

    def on_destroy(self, window):
        """Signal decorator to handle window destroy"""

        def inner(func):
            self.destroy_handlers.setdefault(window.id, []).append(func)
            return func

        return inner

    def on_property_change(self, *args):
        """Signal decorator to handle window property change

        """
        def inner(func):
            if getattr(args[0], 'id', False):
                wid = args[0].id
                props = args[1:]
            else:
                wid = None
                props = args

            for p in props:
                atom = self.get_atom(p)
                self.property_handlers.setdefault(atom, {}).setdefault(wid, []).append(func)

            def remove():
                for p in props:
                    atom = self.get_atom(p)
                    self.property_handlers[atom][wid].remove(func)

            func.remove = remove
            return func

        return inner

    def get_clients(self):
        result = []
        wids = self.root.get_full_property(self.get_atom('_NET_CLIENT_LIST'), Xatom.WINDOW)

        if wids:
            for wid in wids.value:
                result.append(self.dpy.create_resource_object('window', wid))

        return result

    def get_stacked_clients(self):
        result = []
        wids = self.root.get_full_property(
            self.get_atom('_NET_CLIENT_LIST_STACKING'), Xatom.WINDOW)

        if wids:
            for wid in wids.value:
                result.append(self.dpy.create_resource_object('window', wid))

        return result

    @property
    def current_window(self):
        result = self.root.get_full_property(self.get_atom('_NET_ACTIVE_WINDOW'), Xatom.WINDOW)
        if result:
            return self.dpy.create_resource_object('window', result.value[0])

        return None

    @property
    def current_desktop(self):
        return self.root.get_full_property(
            self.dpy.intern_atom('_NET_CURRENT_DESKTOP'), 0).value[0]

    def get_window_desktop(self, window):
        d = window.get_full_property(self.dpy.intern_atom('_NET_WM_DESKTOP'), 0)
        if d:
            d = d.value[0]
            if d == 0xffffffff:
                return -1
            else:
                return d
        else:
            return None

    def set_current_desktop(self, num):
        if num < 0:
            return

        self._send_event(self.root, self.dpy.intern_atom('_NET_CURRENT_DESKTOP'), [num])
        self.dpy.flush()

    def _send_event(self, window, ctype, data, mask=None):
        data = (data + ([0] * (5 - len(data))))[:5]
        ev = ClientMessage(window=window, client_type=ctype, data=(32, (data)))
        self.root.send_event(ev, event_mask=X.SubstructureRedirectMask)

    def get_window_role(self, window):
        d = window.get_full_property(
            self.dpy.intern_atom('WM_WINDOW_ROLE'), Xatom.STRING)
        if d is None or d.format != 8:
            return None
        else:
            return d.value

    def match_string(self, pattern, data):
        if not data:
            return False

        try:
            r = self.re_cache[pattern]
        except KeyError:
            r = self.re_cache[pattern] = re.compile(pattern)

        return r.match(data)

    def is_match(self, window, name=None, cls=None, role=None, desktop=None):
        match = True
        try:
            wname, wclass = window.get_wm_class()
        except TypeError:
            wname = wclass = None

        if match and name:
            match = self.match_string(name, wname)

        if match and cls:
            match = self.match_string(cls, wclass)

        if match and role:
            match = self.match_string(role, self.get_window_role(window))

        if match and desktop is not None:
            match = self.get_window_desktop(window) == desktop

        return match

    def find_clients(self, clients, **matchers):
        result = []
        for c in clients:
            if self.is_match(c, **matchers):
                result.append(c)

        return result

    def find_client(self, clients, **matchers):
        result = self.find_clients(clients, **matchers)
        try:
            return result[0]
        except IndexError:
            return None

    def handle_create(self, window):
        window.change_attributes(event_mask=X.KeyPressMask |
            X.StructureNotifyMask | X.PropertyChangeMask | X.FocusChangeMask)

        self.event_window = window
        for handler in self.create_handlers:
            handler()

    def run(self):
        self.root.change_attributes(event_mask=X.KeyPressMask | X.SubstructureNotifyMask )

        self.startup = True
        for c in self.get_clients():
            self.handle_create(c)

    def handle_events(self):
        while True:
            try:
                event = self.dpy.next_event()
            except KeyboardInterrupt:
                return True

            try:
                etype = event.type
                if etype == X.KeyPress:
                    try:
                        handler = self.key_handlers[event.window.id][(event.state, event.detail)]
                    except KeyError:
                        pass
                    else:
                        self.event = event
                        self.event_window = event.window
                        handler()

                elif etype == X.KeyRelease:
                    pass

                elif etype == X.CreateNotify:
                    self.event = event
                    self.startup = False
                    self.handle_create(event.window)

                elif etype == X.DestroyNotify:
                    try:
                        handlers = self.destroy_handlers[event.window.id]
                    except KeyError:
                        pass
                    else:
                        self.event = event
                        self.event_window = event.window
                        for h in handlers:
                            h()
                    finally:
                        self._clean_window_data(event.window)

                elif etype == X.PropertyNotify:
                    atom = event.atom
                    if event.state == 0 and atom in self.property_handlers:
                        wphandlers = self.property_handlers[atom]
                        self.event_window = event.window
                        self.event = event
                        if event.window.id in wphandlers:
                            for h in wphandlers[event.window.id]:
                                h()

                        if None in wphandlers:
                            for h in wphandlers[None]:
                                h()

                elif etype == X.FocusIn:
                    try:
                        self.focus_history.remove(event.window)
                    except ValueError:
                        pass

                    self.focus_history.append(event.window)

            except (KeyboardInterrupt, SystemExit):
                return True
            except RestartException:
                return False
            except:
                import logging
                logging.getLogger(__name__).exception('Boo')

    def _clean_window_data(self, window):
        wid = window.id
        if wid in self.key_handlers:
            del self.key_handlers[wid]

        if wid in self.destroy_handlers:
            self.destroy_handlers[wid]

        try:
            self.focus_history.remove(window)
        except ValueError:
            pass

        for atom, whandlers in self.property_handlers.items():
            if wid in whandlers:
                del whandlers[wid]

            if not self.property_handlers[atom]:
                del self.property_handlers[atom]

    def focus_and_raise(self, window):
        self.activate_window_desktop(window)
        window.configure(stack_mode=X.Above)
        window.set_input_focus(X.RevertToPointerRoot, X.CurrentTime)
        self.dpy.flush()

    def place_window_above(self, window):
        window.configure(stack_mode=X.Above)
        self.dpy.flush()

    def place_window_below(self, window):
        window.configure(stack_mode=X.Below)
        self.dpy.flush()

    def activate_window_desktop(self, window):
        wd = self.get_window_desktop(window)
        if wd is not None:
            if self.current_desktop != wd:
                self.set_current_desktop(wd)
                return True
            else:
                return False
        else:
            return None

    def get_atom(self, atom_name):
        return self.dpy.get_atom(atom_name)

    def get_atom_name(self, atom):
        return self.dpy.get_atom_name(atom)

    def get_window_state(self, window):
        state_atom = self.get_atom('_NET_WM_STATE')
        state = window.get_full_property(state_atom, Xatom.ATOM)

        return WindowState(
            state and self.get_atom('_NET_WM_STATE_MAXIMIZED_VERT') in state.value,
            state and self.get_atom('_NET_WM_STATE_MAXIMIZED_HORZ') in state.value,
            state and self.get_atom('_OB_WM_STATE_UNDECORATED') in state.value
        )

    def decorate_window(self, window, decorate=True):
        state_atom = self.get_atom('_NET_WM_STATE')
        undecorated_atom = self.get_atom('_OB_WM_STATE_UNDECORATED')
        self._send_event(window, state_atom, [int(not decorate), undecorated_atom])
        self.dpy.flush()

    def close_window(self, window):
        self._send_event(window, self.get_atom("_NET_CLOSE_WINDOW"), [X.CurrentTime])
        self.dpy.flush()

    def change_window_desktop(self, window, desktop):
        if desktop < 0:
            return

        self._send_event(window, self.get_atom("_NET_WM_DESKTOP"), [desktop])
        self.dpy.flush()

    def clear_handlers(self):
        self.key_handlers.clear()
        self.property_handlers.clear()
        self.create_handlers[:] = []
        self.destroy_handlers.clear()
        self.focus_history[:] = []

        self.root.ungrab_key(X.AnyKey, X.AnyModifier)
        for c in self.get_clients():
            c.ungrab_key(X.AnyKey, X.AnyModifier)


class TestWM(object):
    def on_key(self, key):
        assert isinstance(key, basestring), 'First argument to on_key must be string'
        return lambda func: func

    def on_create(self, *args, **matchers):
        assert matchers or args

        if args:
            assert len(args) == 1
            return args[0]

        if matchers:
            possible_args = set(('cls', 'role', 'name', 'desktop'))
            assert possible_args.union(matchers) == possible_args, \
                'Invalid matcher, must be one of %s' % possible_args

        return lambda func: func

    def on_property_change(self, *args):
        assert all(isinstance(r, basestring) for r in args)
        return lambda func: func

    def on_destroy(self, window):
        return lambda func: func
