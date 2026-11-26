use crate::messages::windows as messages;
use std::{
    ffi::OsStr,
    io,
    os::windows::io::{AsRawHandle, FromRawHandle, OwnedHandle},
    path::Path,
    ptr::{null, null_mut},
    sync::{Arc, mpsc},
};

use anyhow::{Context, Result, bail};
use tokio::sync::{mpsc as async_mpsc, watch};
use tray_icon::{
    Icon, TrayIcon, TrayIconBuilder,
    menu::{Menu, MenuEvent, MenuItem, PredefinedMenuItem},
};
use windows_sys::Win32::{
    Foundation::{WAIT_FAILED, WAIT_OBJECT_0},
    System::{
        Com::{COINIT_APARTMENTTHREADED, COINIT_DISABLE_OLE1DDE, CoInitializeEx, CoUninitialize},
        Threading::{CreateEventW, SetEvent},
    },
    UI::{
        Shell::ShellExecuteW,
        WindowsAndMessaging::{
            DispatchMessageW, IDYES, MB_DEFBUTTON2, MB_ICONERROR, MB_ICONWARNING, MB_OK,
            MB_SETFOREGROUND, MB_YESNO, MSG, MWMO_INPUTAVAILABLE, MessageBoxW,
            MsgWaitForMultipleObjectsEx, PM_REMOVE, PeekMessageW, QS_ALLINPUT, SW_SHOWNORMAL,
            TranslateMessage, WM_QUIT,
        },
    },
};

use crate::{
    autostart::wide,
    control::{Connection, Control, Status},
    desktop::{self, Action, Mode, Notice, Request},
    instance, local_log, state,
};

const TITLE: &str = "Kotoyose Child";

enum Item {
    Show,
    Settings,
    Logs,
    Restart,
    Stop,
}

enum Event {
    Status,
    Menu(Item),
    Notice(Notice),
    WorkerFinished,
}

#[derive(Clone)]
struct Mailbox {
    signal: Arc<OwnedHandle>,
    sender: mpsc::Sender<Event>,
}

impl Mailbox {
    fn new() -> io::Result<(Self, mpsc::Receiver<Event>)> {
        let event = unsafe { CreateEventW(null(), 0, 0, null()) };
        if event.is_null() {
            return Err(io::Error::last_os_error());
        }
        let (sender, receiver) = mpsc::channel();
        Ok((
            Self {
                signal: Arc::new(unsafe { OwnedHandle::from_raw_handle(event) }),
                sender,
            },
            receiver,
        ))
    }

    fn post(&self, event: Event) {
        if self.sender.send(event).is_ok() {
            unsafe { SetEvent(self.signal.as_raw_handle()) };
        }
    }
}

struct WorkerWake(Mailbox);

impl Drop for WorkerWake {
    fn drop(&mut self) {
        self.0.post(Event::WorkerFinished);
    }
}

struct Tray {
    icon: TrayIcon,
    status: MenuItem,
}

fn label(connection: Connection) -> &'static str {
    match connection {
        Connection::Connected => messages::CONNECTED,
        Connection::Trying => messages::CONNECTING,
        Connection::Waiting => messages::WAITING,
        Connection::Unregistered => messages::NEEDS_ENROLLMENT,
        Connection::Stopped => messages::STOPPED,
    }
}

fn symbol(connection: Connection) -> Result<Icon> {
    let light = match connection {
        Connection::Connected => [77, 217, 163, 255],
        Connection::Trying | Connection::Waiting => [252, 200, 91, 255],
        Connection::Stopped => [250, 120, 120, 255],
        Connection::Unregistered => [176, 195, 209, 255],
    };
    let mut pixels = Vec::with_capacity(16 * 16 * 4);
    for y in 0i32..16 {
        for x in 0i32..16 {
            let distance = (2 * x - 15).pow(2) + (2 * y - 15).pow(2);
            pixels.extend(if distance <= 130 {
                light
            } else if distance <= 210 {
                [26, 42, 51, 255]
            } else {
                [0, 0, 0, 0]
            });
        }
    }
    Ok(Icon::from_rgba(pixels, 16, 16)?)
}

impl Tray {
    fn new(mailbox: Mailbox) -> Result<Self> {
        let menu = Menu::new();
        let status = MenuItem::new(messages::SHOW_STATUS, true, None);
        let settings = MenuItem::new(messages::OPEN_SETTINGS, true, None);
        let logs = MenuItem::new(messages::OPEN_LOGS, true, None);
        let restart = MenuItem::new(messages::RESTART, true, None);
        let stop = MenuItem::new(messages::EXIT, true, None);
        menu.append_items(&[
            &status,
            &settings,
            &logs,
            &PredefinedMenuItem::separator(),
            &restart,
            &stop,
        ])?;
        let items = [
            status.id().clone(),
            settings.id().clone(),
            logs.id().clone(),
            restart.id().clone(),
            stop.id().clone(),
        ];
        MenuEvent::set_event_handler(Some(move |event: MenuEvent| {
            let selected = match items.iter().position(|id| id == &event.id) {
                Some(0) => Item::Show,
                Some(1) => Item::Settings,
                Some(2) => Item::Logs,
                Some(3) => Item::Restart,
                Some(4) => Item::Stop,
                _ => return,
            };
            mailbox.post(Event::Menu(selected));
        }));
        let icon = TrayIconBuilder::new()
            .with_tooltip(format!("{TITLE}  {}", label(Connection::Trying)))
            .with_menu(Box::new(menu))
            .with_menu_on_left_click(true)
            .with_icon(symbol(Connection::Trying)?)
            .build()?;
        Ok(Self { icon, status })
    }

    fn refresh(&self, state: &Status) -> Result<()> {
        let label = label(state.connection);
        self.status.set_text(messages::tray_status(label));
        self.icon.set_tooltip(Some(format!("{TITLE}  {label}")))?;
        self.icon.set_icon(Some(symbol(state.connection)?))?;
        Ok(())
    }
}

fn dialog(text: &str, flags: u32) -> i32 {
    unsafe {
        MessageBoxW(
            null_mut(),
            wide(text).as_ptr(),
            wide(TITLE).as_ptr(),
            flags | MB_SETFOREGROUND,
        )
    }
}

pub fn error(message: &str) {
    dialog(message, MB_OK | MB_ICONERROR);
}

fn spawn_dialog(show: impl FnOnce() + Send + 'static) {
    if let Err(error) = std::thread::Builder::new()
        .name("kotoyose-dialog".into())
        .spawn(show)
    {
        tracing::warn!("tray dialog start failed: {}", error.kind());
    }
}

fn show_error(message: String) {
    spawn_dialog(move || error(&message));
}

fn show_status(state: &Status) {
    let mut text = label(state.connection).to_owned();
    if let Some(host) = &state.host {
        text.push_str(&messages::tray_host(host));
    }
    if let Some(reason) = &state.error {
        text.push_str(&messages::tray_last_error(reason));
    }
    spawn_dialog(move || {
        dialog(&text, MB_OK);
    });
}

fn confirm(work: usize, restart: bool) -> Option<bool> {
    if work == 0 {
        return Some(false);
    }
    let prompt = messages::tray_interrupt(restart, work);
    (dialog(&prompt, MB_YESNO | MB_ICONWARNING | MB_DEFBUTTON2) == IDYES).then_some(true)
}

struct Apartment(bool);

impl Apartment {
    fn enter() -> Self {
        Self(
            unsafe {
                CoInitializeEx(
                    null(),
                    (COINIT_APARTMENTTHREADED | COINIT_DISABLE_OLE1DDE) as u32,
                )
            } >= 0,
        )
    }
}

impl Drop for Apartment {
    fn drop(&mut self) {
        if self.0 {
            unsafe { CoUninitialize() };
        }
    }
}

fn shell_open(target: &OsStr) -> Result<()> {
    let _apartment = Apartment::enter();
    let result = unsafe {
        ShellExecuteW(
            null_mut(),
            wide("open").as_ptr(),
            wide(target).as_ptr(),
            null(),
            null(),
            SW_SHOWNORMAL,
        )
    };
    if result as isize <= 32 {
        bail!(messages::shell_error(result as isize));
    }
    Ok(())
}

pub fn open_folder(folder: &Path) -> Result<()> {
    if !folder.is_dir() {
        state::private_dir(folder).context(messages::LOGS_CREATE)?;
    }
    shell_open(folder.as_os_str())
}

fn send(commands: &async_mpsc::Sender<Request>, request: Request) {
    if commands.try_send(request).is_err() {
        show_error(messages::APP_BUSY.into());
    }
}

fn pump(
    tray: &Tray,
    home: &Path,
    state: &Control,
    commands: &async_mpsc::Sender<Request>,
    event: &OwnedHandle,
    mailbox: mpsc::Receiver<Event>,
) -> Result<()> {
    loop {
        let result = unsafe {
            MsgWaitForMultipleObjectsEx(
                1,
                &event.as_raw_handle(),
                u32::MAX,
                QS_ALLINPUT,
                MWMO_INPUTAVAILABLE,
            )
        };
        if result == WAIT_FAILED {
            return Err(io::Error::last_os_error()).context("cannot receive tray events");
        }
        if result == WAIT_OBJECT_0 {
            for event in mailbox.try_iter() {
                match event {
                    Event::Status => {
                        if tray.refresh(&state.status()).is_err() {
                            tracing::warn!("tray icon update failed");
                        }
                    }
                    Event::Notice(Notice::Browser(url)) => {
                        if let Err(problem) = shell_open(OsStr::new(&url)) {
                            show_error(messages::open_error(problem));
                        }
                    }
                    Event::Notice(Notice::Error(message)) => {
                        show_error(messages::action_error(&message))
                    }
                    Event::Menu(Item::Show) => show_status(&state.status()),
                    Event::Menu(Item::Settings) => send(commands, Request::Open),
                    Event::Menu(Item::Logs) => {
                        if let Err(problem) = open_folder(&local_log::folder(home)) {
                            show_error(format!("{problem:#}"));
                        }
                    }
                    Event::Menu(item @ (Item::Restart | Item::Stop)) => {
                        let restart = matches!(item, Item::Restart);
                        let work = state.status().work;
                        let commands = commands.clone();
                        let request = move || {
                            if let Some(interrupt) = confirm(work, restart) {
                                send(
                                    &commands,
                                    Request::Action {
                                        action: Action::Exit { restart },
                                        interrupt,
                                        reply: None,
                                    },
                                );
                            }
                        };
                        if work == 0 {
                            request();
                        } else {
                            spawn_dialog(request);
                        }
                    }
                    Event::WorkerFinished => return Ok(()),
                }
            }
        }
        let mut message = MSG::default();
        while unsafe { PeekMessageW(&mut message, null_mut(), 0, 0, PM_REMOVE) } != 0 {
            if message.message == WM_QUIT {
                return Ok(());
            }
            unsafe {
                TranslateMessage(&message);
                DispatchMessageW(&message);
            }
        }
    }
}

pub fn run(background: bool) -> Result<()> {
    let _apartment = Apartment::enter();
    let home = state::home()?;
    let Some((instance, listener)) = instance::claim(&home)? else {
        return Ok(());
    };
    let (mailbox, receiver) = Mailbox::new()?;
    let tray = Tray::new(mailbox.clone()).context("cannot start tray")?;
    let status_mailbox = mailbox.clone();
    let state = Arc::new(Control::new(move || status_mailbox.post(Event::Status)));
    let (commands, inbox) = async_mpsc::channel(8);
    Mode::Tray.request_initial_page(&home, &commands);
    let (stop, stopping) = watch::channel(false);
    let worker = {
        let (home, state, commands, stop, mailbox) = (
            home.clone(),
            state.clone(),
            commands.clone(),
            stop.clone(),
            mailbox.clone(),
        );
        std::thread::Builder::new()
            .name("kotoyose-link".into())
            .spawn(move || {
                let _wake = WorkerWake(mailbox.clone());
                let runtime = crate::runtime()?;
                let local = tokio::task::LocalSet::new();
                runtime.block_on(local.run_until(async move {
                    let _listener = listener.listen(commands.clone(), stop.clone())?;
                    if !background {
                        tokio::task::spawn_local(async move {
                            if crate::signal().await.is_ok() {
                                let _ = stop.send(true);
                            }
                        });
                    }
                    desktop::run(
                        home,
                        state,
                        commands,
                        inbox,
                        stopping,
                        Mode::Tray,
                        |notice| mailbox.post(Event::Notice(notice)),
                    )
                    .await
                }))
            })?
    };
    let result = pump(&tray, &home, &state, &commands, &mailbox.signal, receiver);
    let _ = stop.send(true);
    let exit = worker
        .join()
        .map_err(|_| anyhow::anyhow!("link worker panicked"))??;
    drop(tray);
    drop(instance);
    result?;
    exit.finish(&home, (!background).then_some("run"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Duration;

    #[test]
    fn launching_a_modal_dialog_returns_while_it_is_still_open() {
        let (dismiss, dismissed) = mpsc::channel();
        let (shown, showing) = mpsc::channel();
        let (returned, finished) = mpsc::channel();
        std::thread::spawn(move || {
            spawn_dialog(move || {
                shown.send(()).unwrap();
                let _ = dismissed.recv();
            });
            returned.send(()).unwrap();
        });
        showing.recv_timeout(Duration::from_secs(2)).unwrap();
        finished.recv_timeout(Duration::from_secs(2)).unwrap();
        dismiss.send(()).unwrap();
    }
}
