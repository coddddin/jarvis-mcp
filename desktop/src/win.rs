//! Small Windows helpers: an error box and opening a web link in the default browser.
use tauri::Url;

fn wide(text: &str) -> Vec<u16> {
    text.encode_utf16().chain(std::iter::once(0)).collect()
}

pub fn error_box(text: &str) {
    use windows_sys::Win32::UI::WindowsAndMessaging::{MessageBoxW, MB_ICONERROR, MB_OK};
    let (text, title) = (wide(text), wide("J.A.R.V.I.S."));
    unsafe { MessageBoxW(std::ptr::null_mut(), text.as_ptr(), title.as_ptr(), MB_OK | MB_ICONERROR) };
}

/// Only http and https links go to the browser; other schemes (file, javascript, custom) are dropped.
pub fn is_web_link(url: &Url) -> bool {
    matches!(url.scheme(), "http" | "https") && url.host().is_some()
}

pub fn open_in_browser(url: &Url) {
    use windows_sys::Win32::UI::Shell::ShellExecuteW;
    use windows_sys::Win32::UI::WindowsAndMessaging::SW_SHOWNORMAL;
    if !is_web_link(url) {
        return;
    }
    let (verb, target) = (wide("open"), wide(url.as_str()));
    unsafe {
        ShellExecuteW(
            std::ptr::null_mut(),
            verb.as_ptr(),
            target.as_ptr(),
            std::ptr::null(),
            std::ptr::null(),
            SW_SHOWNORMAL,
        )
    };
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_http_and_https_links_leave_the_app() {
        for good in ["https://example.com/a", "http://example.com"] {
            assert!(is_web_link(&Url::parse(good).unwrap()), "{good}");
        }
        for bad in ["file:///C:/Windows/system32/calc.exe", "javascript:alert(1)", "ms-settings:privacy", "data:text/html,x"] {
            assert!(!is_web_link(&Url::parse(bad).unwrap()), "{bad}");
        }
    }
}
