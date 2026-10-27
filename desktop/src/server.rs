//! The HUD server as a child process: found in the repository, started with `--shell`, tied to a job object.
//!
//! The job object has KILL_ON_JOB_CLOSE, so when this app exits for any reason (including a crash or
//! Task Manager), Windows closes the job handle and ends the server and anything it started.
//! The server hands over its one-time connect URL on the stdout pipe; this module never logs it.
use std::collections::VecDeque;
use std::io::{self, BufRead, BufReader, Read};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::{mpsc, Arc, Mutex};
use std::time::Duration;

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

#[cfg(windows)]
pub struct Job(windows_sys::Win32::Foundation::HANDLE);

// The handle is only closed once, in Drop; Windows job handles may be used from any thread.
#[cfg(windows)]
unsafe impl Send for Job {}
#[cfg(windows)]
unsafe impl Sync for Job {}

#[cfg(windows)]
impl Job {
    pub fn kill_on_close() -> io::Result<Self> {
        use windows_sys::Win32::System::JobObjects::*;
        let handle = unsafe { CreateJobObjectW(std::ptr::null(), std::ptr::null()) };
        if handle.is_null() {
            return Err(io::Error::last_os_error());
        }
        let job = Job(handle);
        let mut info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        let ok = unsafe {
            SetInformationJobObject(
                job.0,
                JobObjectExtendedLimitInformation,
                &info as *const _ as *const core::ffi::c_void,
                std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
            )
        };
        if ok == 0 {
            return Err(io::Error::last_os_error());
        }
        Ok(job)
    }

    pub fn assign(&self, child: &Child) -> io::Result<()> {
        use std::os::windows::io::AsRawHandle;
        let ok = unsafe {
            windows_sys::Win32::System::JobObjects::AssignProcessToJobObject(self.0, child.as_raw_handle() as _)
        };
        if ok == 0 {
            return Err(io::Error::last_os_error());
        }
        Ok(())
    }
}

#[cfg(windows)]
impl Drop for Job {
    fn drop(&mut self) {
        unsafe { windows_sys::Win32::Foundation::CloseHandle(self.0) };
    }
}

/// A running server. Dropping it closes the job, which ends the server process tree.
pub struct Server {
    pub child: Child,
    pub url: String,
    _job: Job,
}

impl Server {
    pub fn stop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
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

/// Start `python -m jarvis.cli.hud --shell` in a new kill-on-close job and wait for its READY line.
/// The error text is for the user: it names the cause and the last server error lines, never the code.
pub fn start(root: &Path, python: &Path, port: u16) -> Result<Server, String> {
    let job = Job::kill_on_close().map_err(|e| format!("작업 개체를 만들지 못했습니다: {e}"))?;
    let mut command = Command::new(python);
    command
        .args(["-m", "jarvis.cli.hud", "--shell", "--port", &port.to_string()])
        .current_dir(root)
        .env("PYTHONIOENCODING", "utf-8")
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x0800_0000); // CREATE_NO_WINDOW
    }
    let mut child = command.spawn().map_err(|e| format!("Python을 시작하지 못했습니다 ({}): {e}", python.display()))?;
    // Assigned right after spawn, before the server has started anything; its later children inherit the job.
    if let Err(e) = job.assign(&child) {
        let _ = child.kill();
        return Err(format!("서버를 작업 개체에 넣지 못했습니다: {e}"));
    }
    let errors: Arc<Mutex<VecDeque<String>>> = Arc::default();
    let kept = errors.clone();
    drain(child.stderr.take().expect("piped stderr"), move |line| {
        let mut kept = kept.lock().unwrap();
        if kept.len() == STDERR_KEEP {
            kept.pop_front();
        }
        kept.push_back(line.trim_end().to_string());
    });
    let (ready, wait) = mpsc::channel();
    drain(child.stdout.take().expect("piped stdout"), move |line| {
        if let Some(url) = parse_ready(&line, port) {
            let _ = ready.send(url);
        }
    });
    match wait.recv_timeout(READY_TIMEOUT) {
        Ok(url) => Ok(Server { child, url, _job: job }),
        Err(timeout) => {
            let _ = child.kill();
            let _ = child.wait();
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

    #[cfg(windows)]
    #[test]
    fn closing_the_job_ends_the_child_process() {
        let job = Job::kill_on_close().unwrap();
        let mut child = Command::new("cmd")
            .args(["/c", "ping", "-n", "30", "127.0.0.1"])
            .stdout(Stdio::null())
            .spawn()
            .unwrap();
        job.assign(&child).unwrap();
        assert!(child.try_wait().unwrap().is_none());
        drop(job);
        let deadline = std::time::Instant::now() + Duration::from_secs(5);
        while child.try_wait().unwrap().is_none() {
            assert!(std::time::Instant::now() < deadline, "child survived the job handle");
            std::thread::sleep(Duration::from_millis(50));
        }
    }
}
