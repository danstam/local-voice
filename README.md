# Local Voice

A floating desktop widget that records and transcribes your speech locally. No cloud. No account. After setup and model download, normal use is fully offline and no audio ever leaves your machine.

        
<div dir="rtl">
<br>
<br>

הכלי פותח כדי לייעל את תהליך העבודה מול סוכנים (Agents). כתיבת הנחיות טכניות מורכבות ומפורטות – כגון תכנון ארכיטקטורה או הגדרת מבנה קוד – דורשת זמן הקלדה רב, בעוד שהמרת קול לטקסט מקצרת את התהליך ומשפרת את הפרודוקטיביות.



**שימו לב:** יש לקחת בחשבון שאיכות התרגום תלויה לחלוטין במודל הלוקאלי (Whisper). יכולות הפענוח והתרגום של המודל בעברית עדיין אינן ברמה של אנגלית, ולכן ייתכנו אי-דיוקים. לקבלת פלט אופטימלי, ההמלצה הטכנית היא עדיין להקליט את האודיו מראש באנגלית.
</div>
<br>


## Setup

**Mac**
```bash
git clone https://github.com/danstam/local-voice.git
cd local-voice
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
brew install ffmpeg
```

**Windows**
```powershell
git clone https://github.com/danstam/local-voice.git
cd local-voice
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
winget install Gyan.FFmpeg
```

---

## Download a model (required once)

Before first use, download at least one Whisper model. After that, transcription runs offline.

**Mac**
```bash
./voice --download-model <model-name>
```

**Windows**
```powershell
.\voice_windows.bat --download-model <model-name>
```

| Model | Size | Recommended for |
|---|---|---|
| `small` | ~461 MB |  מומלץ למי שמדבר אנגלית ברורה...מאוד מהיר ולפעמים לא מדוייק|
| `turbo` | ~1.6 GB | עובד רק באנגלית אך היחס בין איכות ומהירות הכי משתלם|
| `medium` | ~1.4 GB | הכי מתאים למי שמעוניין לדבר בעברית ולקבל תרגום לאנגלית |
| `large-v3` | ~3.1 GB | האיכות הכי גבוהה מכל הבחינות אך כבד מדי לרוב החומרה ואיטי ברמה שזה לא שווה את זה  |

Replace `<model-name>` with one of: `small`, `turbo`, `medium`, or `large-v3`.



---

## Run

**macOS**
```bash
./voice
```

**Windows**
```powershell
.\voice_windows.bat
```

---

