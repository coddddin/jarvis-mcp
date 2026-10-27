
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$w = 'C:\Users\heejun\AppData\Local\JarvisVoiceSpike\work'
$lines = [System.IO.File]::ReadAllLines("$w\sentences.txt", [System.Text.Encoding]::UTF8)
$t0 = [Diagnostics.Stopwatch]::StartNew()
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$s.SelectVoice('Microsoft Heami Desktop')
$init = $t0.Elapsed.TotalMilliseconds
$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)
$i = 0
foreach ($line in $lines) {
  $i++
  $path = "$w\heami-$i.wav"
  $sw = [Diagnostics.Stopwatch]::StartNew()
  $s.SetOutputToWaveFile($path, $fmt)
  $s.Speak($line)
  $s.SetOutputToNull()
  $ms = $sw.Elapsed.TotalMilliseconds
  $bytes = (Get-Item $path).Length - 44
  $dur = $bytes / 32000.0
  "{0}`tsynth_ms={1:N0}`taudio_s={2:N2}`tRTF={3:N3}" -f $i, $ms, $dur, ($ms/1000/$dur)
}
"init_ms={0:N0}`tworking_set_MB={1:N0}" -f $init, ((Get-Process -Id $PID).WorkingSet64/1MB)
