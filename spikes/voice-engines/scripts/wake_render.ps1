
Add-Type -AssemblyName System.Speech
$w = 'C:\Users\heejun\AppData\Local\JarvisVoiceSpike\work\wake'
$names = @{ zira = 'Microsoft Zira Desktop'; heami = 'Microsoft Heami Desktop' }
$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
foreach ($line in [System.IO.File]::ReadAllLines("$w\clips.tsv", [System.Text.Encoding]::UTF8)) {
  $p = $line.Split("`t"); $s.SelectVoice($names[$p[0]])
  $s.SetOutputToWaveFile("$w\$($p[1]).wav", $fmt); $s.Speak($p[2]); $s.SetOutputToNull()
}
