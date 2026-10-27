// The mini ring draws with the same orb as /assistant: copy the one source into the bundled pages.
fn main() {
    let orb = std::path::Path::new("../frontend/assistant/orb.js");
    println!("cargo:rerun-if-changed={}", orb.display());
    std::fs::copy(orb, "dist/orb.js").expect("copy frontend/assistant/orb.js into dist/");
    // App commands get generated allow-<name> permissions; capabilities/ grants each to one window only.
    let manifest = tauri_build::AppManifest::new().commands(&["presence", "expand", "drag"]);
    tauri_build::try_build(tauri_build::Attributes::new().app_manifest(manifest)).expect("tauri build");
}
