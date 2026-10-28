//! App settings kept on this PC in %LOCALAPPDATA%/dev.jarvis.desktop/settings.json. No secrets.
//! A missing or unreadable file, or an unknown value, gives the defaults; unknown keys are kept on save.
use std::path::Path;

use serde_json::{Map, Value};

pub const FILE_NAME: &str = "settings.json";

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Settings {
    /// Start the server with `--voice`. On by default: without the voice packages the server says
    /// "Voice is off" and runs as before.
    pub voice: bool,
    others: Map<String, Value>,
}

impl Default for Settings {
    fn default() -> Self {
        Settings { voice: true, others: Map::new() }
    }
}

impl Settings {
    pub fn parse(text: &str) -> Settings {
        let Ok(Value::Object(mut map)) = serde_json::from_str::<Value>(text) else {
            return Settings::default();
        };
        let voice = match map.remove("voice") {
            Some(Value::Bool(on)) => on,
            _ => true,
        };
        Settings { voice, others: map }
    }

    pub fn render(&self) -> String {
        let mut map = self.others.clone();
        map.insert("voice".into(), Value::Bool(self.voice));
        serde_json::to_string_pretty(&Value::Object(map)).expect("a JSON object always serializes") + "\n"
    }

    pub fn load(dir: &Path) -> Settings {
        std::fs::read_to_string(dir.join(FILE_NAME)).map(|text| Settings::parse(&text)).unwrap_or_default()
    }

    /// Written to a temporary file first, so a crash never leaves half a file.
    pub fn save(&self, dir: &Path) -> std::io::Result<()> {
        std::fs::create_dir_all(dir)?;
        let temporary = dir.join(format!("{FILE_NAME}.tmp"));
        std::fs::write(&temporary, self.render())?;
        std::fs::rename(&temporary, dir.join(FILE_NAME))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn voice_is_on_unless_the_file_says_false() {
        assert!(Settings::parse("").voice);
        assert!(Settings::parse("not json").voice);
        assert!(Settings::parse("[false]").voice);
        assert!(Settings::parse(r#"{"voice": "no"}"#).voice);
        assert!(Settings::parse(r#"{"voice": true}"#).voice);
        assert!(!Settings::parse(r#"{"voice": false}"#).voice);
    }

    #[test]
    fn saving_keeps_unknown_keys_and_round_trips() {
        let mut settings = Settings::parse(r#"{"voice": true, "later": 3}"#);
        settings.voice = false;
        let text = settings.render();
        assert_eq!(Settings::parse(&text), settings);
        assert!(text.contains("\"later\": 3"), "{text}");

        let dir = std::env::temp_dir().join(format!("jarvis-desktop-settings-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        assert!(Settings::load(&dir).voice, "a missing file gives the default");
        settings.save(&dir).unwrap();
        assert_eq!(Settings::load(&dir), settings);
        assert!(!dir.join(format!("{FILE_NAME}.tmp")).exists());
        let _ = std::fs::remove_dir_all(dir);
    }
}
