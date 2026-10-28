//! The HUD server as a child process: found in the repository, started with `--shell`, created inside a job object.
//!
//! The job has KILL_ON_JOB_CLOSE, so when this app exits for any reason (including a crash or Task Manager),
//! Windows closes the job handle and ends the server and anything it started.
//! The child is created already in the job (PROC_THREAD_ATTRIBUTE_JOB_LIST, Windows 10+), so it never runs outside
//! it, and it inherits only its three pipe ends (PROC_THREAD_ATTRIBUTE_HANDLE_LIST). std::process::Command cannot do
//! either on stable Rust, hence CreateProcessW here.
//! The server hands over its one-time connect URL on the stdout pipe; this module never logs it.
use std::collections::VecDeque;
use std::fs::File;
use std::io::{self, BufRead, BufReader, Read};
use std::os::windows::io::{AsRawHandle, FromRawHandle, OwnedHandle};
use std::path::{Path, PathBuf};
use std::sync::{mpsc, Arc, Mutex};
use std::time::Duration;

use windows_sys::Win32::Foundation::{CloseHandle, SetHandleInformation, HANDLE, HANDLE_FLAG_INHERIT, WAIT_OBJECT_0};
use windows_sys::Win32::Security::SECURITY_ATTRIBUTES;
use windows_sys::Win32::System::JobObjects::*;
use windows_sys::Win32::System::Pipes::CreatePipe;
use windows_sys::Win32::System::Threading::*;

pub const DEFAULT_PORT: u16 = 8787;
const READY_TIMEOUT: Duration = Duration::from_secs(120);
const STDERR_KEEP: usize = 8;

/// A repository root has the stable entry point and the HUD module.
pub fn is_root(dir: &Path) -> bool {
    dir.join("server.py").is_file() && dir.join("jarvis").join("cli").join("hud.py").is_file()
}

/// `JARVIS_ROOT` if it is a root, otherwise the nearest ancestor of the executable that is one.
pub fn find_root(exe: &Path, env_root: Option<PathBuf>) -> Option<PathBuf> {
    if let Some(root) = env_root {
        return is_root(&root).then_some(root);
    }
    exe.ancestors().skip(1).find(|dir| is_root(dir)).map(Path::to_path_buf)
}

/// `JARVIS_PYTHON` if set, otherwise the repository virtual environment.
pub fn find_python(root: &Path, env_python: Option<PathBuf>) -> Option<PathBuf> {
    let python = env_python.unwrap_or_else(|| root.join(".venv").join("Scripts").join("python.exe"));
    python.is_file().then_some(python)
}

/// Accept only `READY http://127.0.0.1:<port>/assistant#connect=<43 url-safe chars>`.
pub fn parse_ready(line: &str, port: u16) -> Option<String> {
    let url = line.trim_end().strip_prefix("READY ")?;
    let code = url.strip_prefix(&format!("http://127.0.0.1:{port}/assistant#connect="))?;
    let valid = code.len() == 43 && code.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_');
    valid.then(|| url.to_string())
}

/// One argument for a Windows command line, quoted so the C runtime's parser gives it back unchanged.
fn quote_arg(arg: &str, out: &mut String) {
    if !arg.is_empty() && !arg.contains([' ', '\t', '"']) {
        out.push_str(arg);
        return;
    }
    out.push('"');
    let mut backslashes = 0;
    for c in arg.chars() {
        if c == '\\' {
            backslashes += 1;
            continue;
        }
        let count = if c == '"' { backslashes * 2 + 1 } else { backslashes };
        out.extend(std::iter::repeat_n('\\', count));
        out.push(c);
        backslashes = 0;
    }
    out.extend(std::iter::repeat_n('\\', backslashes * 2));
    out.push('"');
}

/// The program path is always quoted and never escaped (paths cannot contain quotes); then each argument.
fn command_line(program: &Path, args: &[&str]) -> String {
    let mut line = format!("\"{}\"", program.display());
    for arg in args {
        line.push(' ');
        quote_arg(arg, &mut line);
    }
    line
}

fn wide(text: &str) -> Vec<u16> {
    text.encode_utf16().chain(std::iter::once(0)).collect()
}

fn check(ok: i32) -> io::Result<()> {
    if ok == 0 {
        Err(io::Error::last_os_error())
    } else {
        Ok(())
    }
}

pub struct Job(HANDLE);

// The handle is only closed once, in Drop; Windows job handles may be used from any thread.
unsafe impl Send for Job {}
unsafe impl Sync for Job {}

impl Job {
    pub fn kill_on_close() -> io::Result<Self> {
        let handle = unsafe { CreateJobObjectW(std::ptr::null(), std::ptr::null()) };
        if handle.is_null() {
            return Err(io::Error::last_os_error());
        }
        let job = Job(handle);
        let mut info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        check(unsafe {
            SetInformationJobObject(
                job.0,
                JobObjectExtendedLimitInformation,
                &info as *const _ as *const core::ffi::c_void,
                std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
            )
        })?;
        Ok(job)
    }

    #[cfg(test)]
    pub fn contains(&self, process: &Process) -> bool {
        let mut inside = 0;
        let ok = unsafe { IsProcessInJob(process.handle.as_raw_handle() as HANDLE, self.0, &mut inside) };
        ok != 0 && inside != 0
    }
}

impl Drop for Job {
    fn drop(&mut self) {
        unsafe { CloseHandle(self.0) };
    }
}

/// A child process handle (std::process::Child cannot be built from one).
pub struct Process {
    handle: OwnedHandle,
}

impl Process {
    /// The exit code once the process has ended, without waiting.
    pub fn try_wait(&self) -> Option<u32> {
        self.wait(Duration::ZERO)
    }

    pub fn wait(&self, limit: Duration) -> Option<u32> {
        let handle = self.handle.as_raw_handle() as HANDLE;
        if unsafe { WaitForSingleObject(handle, limit.as_millis().min(u32::MAX as u128 - 1) as u32) } != WAIT_OBJECT_0 {
            return None;
        }
        let mut code = 0;
        (unsafe { GetExitCodeProcess(handle, &mut code) } != 0).then_some(code)
    }

    pub fn kill(&self) {
        unsafe { TerminateProcess(self.handle.as_raw_handle() as HANDLE, 1) };
    }
}

pub struct Spawned {
    pub process: Process,
    pub stdout: File,
    pub stderr: File,
}

/// An anonymous pipe: (read, write). Both ends start inheritable; the caller clears the parent's end.
fn pipe() -> io::Result<(OwnedHandle, OwnedHandle)> {
    let attributes = SECURITY_ATTRIBUTES {
        nLength: std::mem::size_of::<SECURITY_ATTRIBUTES>() as u32,
        lpSecurityDescriptor: std::ptr::null_mut(),
        bInheritHandle: 1,
    };
    let (mut read, mut write) = (std::ptr::null_mut(), std::ptr::null_mut());
    check(unsafe { CreatePipe(&mut read, &mut write, &attributes, 0) })?;
    Ok(unsafe { (OwnedHandle::from_raw_handle(read as _), OwnedHandle::from_raw_handle(write as _)) })
}

fn keep_private(handle: &OwnedHandle) -> io::Result<()> {
    check(unsafe { SetHandleInformation(handle.as_raw_handle() as HANDLE, HANDLE_FLAG_INHERIT, 0) })
}

/// Start `program args` with no console window, inside `job` from its first instruction, with piped stdout and
/// stderr and an empty stdin. Only the three child pipe ends are inherited.
pub fn spawn_in_job(job: &Job, program: &Path, args: &[&str], dir: &Path) -> io::Result<Spawned> {
    let (stdin_child, stdin_parent) = pipe()?;
    let (stdout_parent, stdout_child) = pipe()?;
    let (stderr_parent, stderr_child) = pipe()?;
    for parent_end in [&stdin_parent, &stdout_parent, &stderr_parent] {
        keep_private(parent_end)?;
    }
    let inherited: [HANDLE; 3] =
        [stdin_child.as_raw_handle() as _, stdout_child.as_raw_handle() as _, stderr_child.as_raw_handle() as _];
    let jobs: [HANDLE; 1] = [job.0];

    // The attribute list is an opaque buffer; usize elements keep it pointer aligned.
    let mut size = 0;
    unsafe { InitializeProcThreadAttributeList(std::ptr::null_mut(), 2, 0, &mut size) };
    let mut buffer = vec![0usize; size.div_ceil(std::mem::size_of::<usize>())];
    let list = buffer.as_mut_ptr() as LPPROC_THREAD_ATTRIBUTE_LIST;
    check(unsafe { InitializeProcThreadAttributeList(list, 2, 0, &mut size) })?;
    struct ListGuard(LPPROC_THREAD_ATTRIBUTE_LIST);
    impl Drop for ListGuard {
        fn drop(&mut self) {
            unsafe { DeleteProcThreadAttributeList(self.0) };
        }
    }
    let _guard = ListGuard(list);
    // `inherited` and `jobs` outlive the list: both are dropped after `_guard`.
    check(unsafe {
        UpdateProcThreadAttribute(
            list,
            0,
            PROC_THREAD_ATTRIBUTE_HANDLE_LIST as usize,
            inherited.as_ptr() as *const core::ffi::c_void,
            std::mem::size_of_val(&inherited),
            std::ptr::null_mut(),
            std::ptr::null(),
        )
    })?;
    check(unsafe {
        UpdateProcThreadAttribute(
            list,
            0,
            PROC_THREAD_ATTRIBUTE_JOB_LIST as usize,
            jobs.as_ptr() as *const core::ffi::c_void,
            std::mem::size_of_val(&jobs),
            std::ptr::null_mut(),
            std::ptr::null(),
        )
    })?;

    let mut info = STARTUPINFOEXW::default();
    info.StartupInfo.cb = std::mem::size_of::<STARTUPINFOEXW>() as u32;
    info.StartupInfo.dwFlags = STARTF_USESTDHANDLES;
    info.StartupInfo.hStdInput = inherited[0];
    info.StartupInfo.hStdOutput = inherited[1];
    info.StartupInfo.hStdError = inherited[2];
    info.lpAttributeList = list;
    let mut line = wide(&command_line(program, args));
    let dir = wide(&dir.display().to_string());
    let mut created = PROCESS_INFORMATION::default();
    check(unsafe {
        CreateProcessW(
            std::ptr::null(),
            line.as_mut_ptr(),
            std::ptr::null(),
            std::ptr::null(),
            1, // required with PROC_THREAD_ATTRIBUTE_HANDLE_LIST; only the listed handles are inherited
            EXTENDED_STARTUPINFO_PRESENT | CREATE_NO_WINDOW,
            std::ptr::null(),
            dir.as_ptr(),
            &info as *const STARTUPINFOEXW as *const STARTUPINFOW,
            &mut created,
        )
    })?;
    unsafe { CloseHandle(created.hThread) };
    // The child has its own copies; closing ours lets reads end when the child exits, and gives it an empty stdin.
    drop((stdin_child, stdout_child, stderr_child, stdin_parent));
    Ok(Spawned {
        process: Process { handle: unsafe { OwnedHandle::from_raw_handle(created.hProcess as _) } },
        stdout: File::from(stdout_parent),
        stderr: File::from(stderr_parent),
    })
}

/// A running server. Dropping it closes the job, which ends the server process tree.
pub struct Server {
    pub child: Process,
    pub url: String,
    _job: Job,
}

impl Server {
    /// The exit code if the server has ended.
    pub fn exited(&self) -> Option<u32> {
        self.child.try_wait()
    }

    pub fn stop(&mut self) {
        self.child.kill();
        let _ = self.child.wait(Duration::from_secs(5));
    }
}

fn drain<R: Read + Send + 'static>(stream: R, mut each: impl FnMut(String) + Send + 'static) {
    std::thread::spawn(move || {
        let mut reader = BufReader::new(stream);
        let mut buf = Vec::new();
        loop {
            buf.clear();
            match reader.read_until(b'\n', &mut buf) {
                Ok(0) | Err(_) => break,
                Ok(_) => each(String::from_utf8_lossy(&buf).into_owned()),
            }
        }
    });
}

/// The server's arguments. `--voice` adds local push-to-talk and spoken replies; the server loads the speech models
/// itself, in the background after it is ready, and runs without voice when the voice packages are missing.
fn hud_args(port: &str, voice: bool) -> Vec<&str> {
    let mut args = vec!["-X", "utf8", "-m", "jarvis.cli.hud", "--shell", "--port", port];
    if voice {
        args.push("--voice");
    }
    args
}

/// Start `python -X utf8 -m jarvis.cli.hud --shell [--voice]` in a new kill-on-close job and wait for its READY line.
/// The error text is for the user: it names the cause and the last server error lines, never the code.
pub fn start(root: &Path, python: &Path, port: u16, voice: bool) -> Result<Server, String> {
    let job = Job::kill_on_close().map_err(|e| format!("작업 개체를 만들지 못했습니다: {e}"))?;
    let port_text = port.to_string();
    let args = hud_args(&port_text, voice);
    let spawned = spawn_in_job(&job, python, &args, root)
        .map_err(|e| format!("Python을 시작하지 못했습니다 ({}): {e}", python.display()))?;
    let errors: Arc<Mutex<VecDeque<String>>> = Arc::default();
    let kept = errors.clone();
    drain(spawned.stderr, move |line| {
        let mut kept = kept.lock().unwrap();
        if kept.len() == STDERR_KEEP {
            kept.pop_front();
        }
        kept.push_back(line.trim_end().to_string());
    });
    let (ready, wait) = mpsc::channel();
    drain(spawned.stdout, move |line| {
        if let Some(url) = parse_ready(&line, port) {
            let _ = ready.send(url);
        }
    });
    let child = spawned.process;
    match wait.recv_timeout(READY_TIMEOUT) {
        Ok(url) => Ok(Server { child, url, _job: job }),
        Err(timeout) => {
            child.kill();
            let _ = child.wait(Duration::from_secs(5));
            let tail = errors.lock().unwrap().iter().cloned().collect::<Vec<_>>().join("\n");
            let cause = match timeout {
                mpsc::RecvTimeoutError::Timeout => "서버가 제한 시간 안에 준비되지 않았습니다.".to_string(),
                mpsc::RecvTimeoutError::Disconnected => {
                    format!("서버가 준비 전에 종료됐습니다. 포트 {port}를 다른 프로그램이 쓰고 있을 수 있습니다.")
                }
            };
            Err(if tail.is_empty() { cause } else { format!("{cause}\n\n{tail}") })
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const CODE: &str = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJ0123-_Z";

    fn cmd() -> PathBuf {
        PathBuf::from(std::env::var("SystemRoot").unwrap()).join("System32").join("cmd.exe")
    }

    #[test]
    fn ready_line_must_be_exact_loopback_assistant_url_with_a_valid_code() {
        let good = format!("READY http://127.0.0.1:8787/assistant#connect={CODE}\r\n");
        assert_eq!(parse_ready(&good, 8787).as_deref(), Some(good.trim_end().strip_prefix("READY ").unwrap()));
        for bad in [
            format!("READY http://127.0.0.1:8788/assistant#connect={CODE}"),
            format!("READY http://localhost:8787/assistant#connect={CODE}"),
            format!("READY http://127.0.0.1:8787/#connect={CODE}"),
            format!("READY http://127.0.0.1:8787/assistant#connect={}", &CODE[1..]),
            format!("READY http://127.0.0.1:8787/assistant#connect={}x", CODE),
            format!("READY http://127.0.0.1:8787/assistant#connect={}", CODE.replace('Z', "%")),
            format!("J.A.R.V.I.S. local workspace: http://127.0.0.1:8787/assistant#connect={CODE}"),
        ] {
            assert_eq!(parse_ready(&bad, 8787), None, "{bad}");
        }
    }

    #[test]
    fn root_comes_from_env_or_the_nearest_ancestor_of_the_executable() {
        let base = std::env::temp_dir().join(format!("jarvis-desktop-root-{}", std::process::id()));
        let root = base.join("repo");
        std::fs::create_dir_all(root.join("jarvis").join("cli")).unwrap();
        std::fs::write(root.join("server.py"), "").unwrap();
        std::fs::write(root.join("jarvis").join("cli").join("hud.py"), "").unwrap();
        let exe = root.join("desktop").join("target").join("release").join("jarvis-desktop.exe");
        assert_eq!(find_root(&exe, None), Some(root.clone()));
        assert_eq!(find_root(&base.join("elsewhere").join("app.exe"), None), None);
        assert_eq!(find_root(&exe, Some(base.clone())), None, "an invalid JARVIS_ROOT is not silently replaced");
        assert_eq!(find_python(&root, None), None);
        let python = root.join(".venv").join("Scripts").join("python.exe");
        std::fs::create_dir_all(python.parent().unwrap()).unwrap();
        std::fs::write(&python, "").unwrap();
        assert_eq!(find_python(&root, None), Some(python));
        let _ = std::fs::remove_dir_all(base);
    }

    #[test]
    fn voice_adds_only_the_voice_flag() {
        let plain = hud_args("8787", false);
        assert_eq!(plain, ["-X", "utf8", "-m", "jarvis.cli.hud", "--shell", "--port", "8787"]);
        let voice = hud_args("8787", true);
        assert_eq!(voice[..plain.len()], plain[..]);
        assert_eq!(voice[plain.len()..], ["--voice"]);
    }

    #[test]
    fn command_line_quotes_like_the_c_runtime_parser() {
        let program = Path::new(r"C:\Program Files\Python\python.exe");
        assert_eq!(
            command_line(program, &["-m", "jarvis.cli.hud", "--port", "8787"]),
            r#""C:\Program Files\Python\python.exe" -m jarvis.cli.hud --port 8787"#
        );
        let mut out = String::new();
        for arg in ["", "a b", r#"say "hi""#, r"dir\", r"dir\ x", r#"a\"b"#] {
            out.clear();
            quote_arg(arg, &mut out);
            let expected = match arg {
                "" => r#""""#,
                "a b" => r#""a b""#,
                r#"say "hi""# => r#""say \"hi\"""#,
                r"dir\" => r"dir\",
                r"dir\ x" => r#""dir\ x""#,
                _ => r#""a\\\"b""#,
            };
            assert_eq!(out, expected, "{arg}");
        }
    }

    #[test]
    fn the_child_is_in_the_job_from_the_start_and_its_output_is_piped() {
        let job = Job::kill_on_close().unwrap();
        let mut spawned = spawn_in_job(&job, &cmd(), &["/c", "echo", "ready"], &std::env::temp_dir()).unwrap();
        assert!(job.contains(&spawned.process), "created inside the job");
        let mut text = String::new();
        spawned.stdout.read_to_string(&mut text).unwrap();
        assert_eq!(text.trim(), "ready");
        assert_eq!(spawned.process.wait(Duration::from_secs(5)), Some(0));
        let other = Job::kill_on_close().unwrap();
        assert!(!other.contains(&spawned.process));
    }

    #[test]
    fn closing_the_job_ends_the_child_and_its_children() {
        let job = Job::kill_on_close().unwrap();
        let spawned = spawn_in_job(&job, &cmd(), &["/c", "ping", "-n", "30", "127.0.0.1"], &std::env::temp_dir()).unwrap();
        assert_eq!(spawned.process.try_wait(), None);
        drop(job);
        assert!(spawned.process.wait(Duration::from_secs(5)).is_some(), "child survived the job handle");
    }
}
