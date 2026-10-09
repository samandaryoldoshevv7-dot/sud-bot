# ⚖️ Sud xodimlarini o'qitish va test qilish Telegram boti

Sud xodimlari uchun **Telegram orqali ishlaydigan** professional o'qitish va imtihon tizimi.
Administrator rasmiy materiallarni (qonunlar, yo'riqnomalar, ichki hujjatlar, yangiliklar) yuklaydi;
tizim ularni RAG (Retrieval-Augmented Generation) orqali indekslaydi va **faqat shu manbalarga
asoslangan** test savollarini Groq LLM yordamida yaratadi. Xodimlar testlarni bot bilan shaxsiy
chatda topshiradi, administrator esa qatnashish, natijalar, xatolar, mavzular, reyting va
Excel hisobotlarni kuzatadi.

> Veb-sayt yo'q. Yagona interfeys — Telegram. Barcha xabarlar o'zbek tilida (lotin).

---

## Mundarija

1. [Imkoniyatlar](#imkoniyatlar)
   - [Aralash test yaratish](#test-yaratish-bir-nechta-fayldan-aralash-test)
   - [Xodim: Testlarim va test jarayoni](#xodim-testlarim-va-test-jarayoni)
   - [Guruhda test](#guruhda-test)
   - [Admin: boshqaruv va natijalar](#admin-boshqaruv-va--natijalar)
2. [Arxitektura](#arxitektura)
3. [Talablar](#talablar)
4. [Muhit o'zgaruvchilari](#muhit-ozgaruvchilari)
5. [Railway'ga joylash (deploy)](#railwayga-joylash)
6. [Telegram botni sozlash](#telegram-botni-sozlash)
7. [Birinchi qadamlar: admin, hujjat, test](#birinchi-qadamlar)
8. [Lokal ishlab chiqish](#lokal-ishlab-chiqish)
9. [Ma'lumotlar bazasi va migratsiyalar](#malumotlar-bazasi-va-migratsiyalar)
10. [AI / RAG pipeline](#ai--rag-pipeline)
11. [Testlar (avtomatik)](#avtomatik-testlar)
12. [Xavfsizlik](#xavfsizlik)
13. [Muammolarni hal qilish](#muammolarni-hal-qilish)
14. [Loyiha tuzilmasi](#loyiha-tuzilmasi)
15. [Ma'lum cheklovlar](#malum-cheklovlar)

---

## Imkoniyatlar

**Administrator (👨‍💼 ADMIN PANEL):**

| Bo'lim | Nima qiladi |
|---|---|
| ➕ Test yaratish | nom → bir nechta fayl → 🔀 aralash test → savollar soni → vaqt (6/12/24 soat, 2 kun) → kimga (hammaga / guruhga / tanlanganlarga / bitta xodimga) |
| 📋 Natijalar | test → xodimlar natijasi → har bir javob (tanlagan, to'g'ri javob, manba); qo'shimcha vaqt, qayta topshirish, testdan olib tashlash |
| 👥 Xodimlar | ro'yxat (sahifalash), qidirish, faollashtirish, **⛔️ bloklash / blokdan chiqarish**, **🗑 o'chirish** (ro'yxatdan yo'qoladi, natijalari hisobotda qoladi; «🗑 O'chirilganlar»dan qayta tiklash yoki ❌ butunlay o'chirish), profil, statistika, zaif/kuchli mavzular, **xato javoblar** tarixi |
| 📚 Materiallar | PDF / DOCX / TXT / MD fayl yoki nusxalangan matn yuklash; avtomatik ajratish → tozalash → bo'laklash → embedding; holat UPLOADED/PROCESSING/READY/FAILED; xato sababi; AI bilan savol yaratish; arxivlash/o'chirish |
| 📰 Yangiliklar | sarlavha, matn, sana, manba, toifa, faol/nofaol; testlarda manba sifatida (masalan 30%) |
| 📝 Testlar | Usta: nom, tavsif, savollar soni, variantlar (3–5), qiyinlik, manbalar, yangiliklar %, mavzu, **qayerda ishlanadi (guruhda yoki shaxsiy chatda)**, boshlanish, muddat, aralashtirish, javoblarni ko'rsatish rejimi (darhol / test oxirida / ko'rsatilmaydi), o'tish bali. **⚡ Tezkor test**: material sahifasidan bir bosishda. Hayot sikli: **DRAFT → READY → ACTIVE → EXPIRED / CLOSED** |
| 👀 Ko'rib chiqish | har bir savol: ✅ Tasdiqlash / ❌ Rad etish / 🔄 Qayta yaratish; manba va iqtibos ko'rinadi. Tasdiqlanmagan savollar bilan test e'lon qilinmaydi |
| 🗂 Savollar banki | qayta ishlatiladigan bank: filtr (holat, mavzu, material, yangilik, qiyinlik), qidirish, tasdiqlash, tahrirlash (versiyalanadi), o'chirish, qayta yaratish, izohni manbadan AI bilan qayta yozish |
| 👥 Ishtirokchilar | har bir test uchun: **Jami / Yakunlagan / Jarayonda / Muddati o'tgan / Bekor / Qatnashmagan**; ball, to'g'ri, xato, vaqt, boshlangan va tugagan vaqt; har bir xodimning **har bir javobi** (🧾 Kim nimani tanladi) va savollar bo'yicha A/B/C/D taqsimoti |
| 📊 Statistika | xodimlar, testlar, urinishlar, o'rtacha ball, qatnashish darajasi, eng qiyin savollar, eng ko'p xatolar, zaif mavzular |
| 🏆 Reyting | kunlik / haftalik / oylik / umumiy (yetarli ma'lumoti yo'q xodimlar reytingga kirmaydi — sozlanadi) |
| 📈 Hisobotlar | XLSX (test bo'yicha: umumiy, ishtirokchilar, qatnashmaganlar, urinishlar, xato javoblar, savollar tahlili; umumiy: natijalar + xodimlar bo'yicha xulosa + testlar) va CSV |
| 👥 Guruhlar | Telegram guruhlarni ro'yxatga olish, a'zolar, test e'lonini guruhga yuborish |
| ⚙️ Sozlamalar | avto-tasdiqlash, xabarnomalar, eslatma vaqti, reyting minimumi va h.k. |

**Xodim:** `📚 Testlarim`, `📊 Natijalarim`, `ℹ️ Yordam` (batafsil quyida). Xodim faqat **o'z**
natijalarini ko'radi.

**Avtomatik (scheduler):** rejalashtirilgan testni vaqtida faollashtirish va e'lon qilish, muddat
tugaganda testni va tugallanmagan urinishlarni yopish, muddatdan oldin eslatma, test tugaganda
adminlarga yakuniy hisobot.

### Test yaratish: bir nechta fayldan aralash test

1. **➕ Test yaratish** → «Test nomini kiriting» → nom.
2. «Test uchun fayllarni yuboring» → bir yoki bir nechta fayl (PDF/DOCX/TXT/MD). Har bir fayl alohida
   **manba** bo'ladi va `test_sources` jadvaliga darhol yoziladi: manba nomi (`Konstitutsiya.pdf` →
   «Konstitutsiya»), asl fayl nomi va Telegram `file_id`. Fayllar fonda o'qiladi (⏳ → ✅).
3. **🔀 Aralashtirib test tuzish** → savollar soni: 10 / 20 / 30 / 40 / 50 / ✍️ maxsus.
4. **⏱ TEST VAQTI** — faqat 4 variant: **6 soat · 12 soat · 24 soat · 2 kun** (standart 24 soat;
   bazada soniyada: 21600 / 43200 / 86400 / 172800).
5. **👥 Kimlarga berilsin?** — barcha xodimlarga / guruhga / tanlangan xodimlarga / bitta xodimga.
6. **✅ Testni yaratish** → bot har bir faylga teng ulush beradi (40 ta / 4 fayl = 10 tadan), avval
   tasdiqlangan bank savollarini oladi, yetmasa faqat **o'sha fayl** matnidan yangi savol tuzadi
   (biror fayl yetarli bermasa, qolgan fayllar to'ldiradi) → test faollashadi → xodimlarning
   «📚 Testlarim» bo'limida paydo bo'ladi va ularga xabar boradi. Hammasi tuzilmasa:
   «⚠️ 40 tadan 32 tasi tuzildi» va **✅ 32 ta savol bilan yuborish** tugmasi.

**Manba qat'iy:** AI javobida `question, options, correct_answer, source, source_file` majburiy.
Backend: (1) Pydantic tekshiruvi; (2) iqtibos aynan o'sha bo'lak (chunk) matnidan topilishi kerak;
(3) AI ko'rsatgan `source/source_file` iqtibos topilgan bo'lakning **haqiqiy** fayliga mos bo'lishi
kerak — bo'sh bo'lsa `missing_source`, boshqa hujjat ko'rsatilsa `source_mismatch` bilan rad etiladi;
(4) ko'r tekshiruv va sifat tekshiruvi. Bazaga AI yozgan nom emas, bo'lakning haqiqiy manbasi yoziladi.
Manbasi aniqlanmagan savol testga qo'shilmaydi.

**Aralashtirish:** savollar manbalar bo'yicha almashinib chiqadi (1 — Konstitutsiya, 2 — Mehnat kodeksi,
3 — Ma'muriy kodeks, 4 — Sug'urta, 5 — Konstitutsiya…). Har bir xodimga o'z tartibi beriladi, lekin
boshqa manbada savol qolgan bo'lsa, bir manbaning savoli ketma-ket kelmaydi. Variantlar ham har bir
xodim uchun aralashtiriladi; ko'rilgan harf → asl variant mapping'i saqlanadi (`selected_display`,
`selected_option`), to'g'ri javob har doim asl harf bo'yicha tekshiriladi.

### Xodim: 📚 Testlarim va test jarayoni

* `/start` → xodim **avtomatik bazaga qo'shiladi va faol bo'ladi** (admin'ga xabar boradi; admin istalgan
  vaqtda ⛔️ bloklashi yoki 🗑 o'chirishi mumkin — bloklangan xodim `/start` bilan qayta ochilmaydi).
  Qo'lda tasdiqlash rejimi ⚙️ Sozlamalar → «Hammani avtomatik tasdiqlash» ni o'chirsa qaytadi.
* **📚 Testlarim**: har bir test holati bilan — 🟢 Ishlanmagan · 🟡 Jarayonda · 🔵 Tugatilgan · 🔴 Muddati tugagan.
* **▶️ TESTNI BOSHLASH** → `started_at = server vaqti`, `deadline = started_at + test vaqti`. Hisob
  faqat server/baza vaqtida; bot qayta ishga tushsa yoki xodim chiqib ketsa ham vaqt davom etadi,
  qaytganda **birinchi javob berilmagan savoldan** davom etadi. Har bir javobda `hozir >= deadline`
  tekshiriladi; scheduler muddati o'tgan urinishlarni yopadi va xodimga `❌ TEST VAQTI TUGADI` + natija yuboradi.
* Savol ko'rinishi (Quiz uslubi): `[3/30] Savol matni` → `📚 Manba: Konstitutsiya` → `○ A) …` variantlar,
  har bir variant alohida tugma.
* Variant bosilganda: xodim `callback.from_user.id` bo'yicha aniqlanadi → **uning** urinishi topiladi →
  javob bazaga yoziladi → tekshiriladi → savol xabari natija oynasiga aylanadi:
  ```
  ❌ NOTO'G'RI JAVOB

  Sizning javobingiz:
  B) Fe

  ✅ TO'G'RI JAVOB:
  A) Co

  📚 Manba: Kimyo
  💡 Izoh: …                       [✖️ CHIQISH]
  ```
  Maqtov so'zlari ishlatilmaydi. Javobni o'zgartirib bo'lmaydi (`⚠️ Bu savolga siz allaqachon javob bergansiz.`).
* **✖️ CHIQISH** testdan chiqish emas: faqat natija oynasini yopadi, urinish va javob saqlanadi, o'sha
  xabar keyingi savolga aylanadi. Oxirgi savoldan keyin — `🎯 TEST YAKUNLANDI` (savollar, to'g'ri,
  noto'g'ri, foiz, sarflangan vaqt) va aralash testda **📚 MANBA BO'YICHA NATIJA**.

### Guruhda test

Admin: test sahifasi → **▶️ Guruhda boshlash** (yoki yaratishda «🏢 Guruhga»). Guruhga bitta xabar
chiqadi: `📚 YANGI TEST` · nom · `📝 30 ta savol` · `⏱ 24 soat` · **[▶️ TESTNI BOSHLASH]**.
Tugma bosilganda xodim faqat `callback.from_user.id` bo'yicha aniqlanadi, unga **alohida urinish**
ochiladi (shaxsiy vaqti shu paytdan boshlanadi) va Telegram bot bilan **shaxsiy chatni** ochadi —
savollar o'sha yerda, boshqa a'zolar javoblarni ko'rmaydi. Ali, Vali, Hasan bir vaqtda bossa ham
har birining urinishi va javoblari alohida (`test_attempts` + `user_answers`, qatorni `FOR UPDATE`
bilan bloklash, `UNIQUE(attempt_id, test_question_id)`).
Oldingi versiyada guruhga savollari bilan yuborilgan testlar tugaguncha avvalgidek ishlaydi.

Yangi a'zolar: bot guruhda «👋 Xush kelibsiz» va faol testlar ro'yxatini tugma bilan yuboradi;
guruhda `/test` faol testlarni ko'rsatadi.

### Admin: boshqaruv va 📋 Natijalar

* Test sahifasi: ⏸ **Deaktiv** / ▶️ **Aktiv**, ⏱ **Vaqtni o'zgartirish** (6/12/24 soat, 2 kun;
  jarayondagilar uchun «boshlagan vaqt + yangi vaqt»), ⛔ **Yopish** (majburiy tugatish),
  🔁 **Qayta ochish**, 🗑 **O'chirish** (natijalari bilan, tasdiqlash so'raladi), Excel hisobot.
* **📋 Natijalar** → test → har bir xodim: `30 ta savol · ✅ 26 · ❌ 4 · 87%` → xodim → har bir savol:
  `Tanladi: A) Co · Natija: ✅ To'g'ri · Manba: Kimyo` (xato bo'lsa to'g'ri javob ham), manba
  bo'yicha natija. Shu yerda: **⏱ Qo'shimcha vaqt** (+6/12/24 soat, +2 kun; vaqti tugagan tugallanmagan
  urinish qayta ochiladi), **🔁 Qayta topshirishga ruxsat** (bir martalik, yangi urinish; eski javoblar
  o'zgarmaydi), **🚫 Testni xodimdan olib tashlash**.
* Excel hisobotda «Manba bo'yicha» varag'i va har bir javobda manba/fayl ustunlari.

---

## Arxitektura

```
Telegram ──(long polling yoki webhook)──► aiogram 3 Dispatcher
                                              │  middlewares: DB sessiya, foydalanuvchi ro'yxati/rol
                                              ▼
           handlers/ (admin/*, employee, group, common)  ── keyboards/, locales/ (UI matnlari)
                                              │
                                              ▼
 services/ (users, groups, materials, question_generation, question_bank, test_builder,
            attempts, notifications, scheduler)   statistics/   reports/
        │                    │                         │
        ▼                    ▼                         ▼
 documents/ (PDF/DOCX/TXT/MD → tozalash → bo'laklash)   ai/ (Groq provayder, promptlar,
 rag/ (fastembed embedding, pgvector / real[] + PostgreSQL full-text, gibrid qidiruv)  JSON validatsiya)
        │
        ▼
 PostgreSQL 16 (+pgvector) — SQLAlchemy 2 (async) + Alembic
```

**Bitta jarayon** ichida: bot (polling yoki webhook), fon scheduler'i (har 60 soniya) va `/health`
HTTP endpoint. Railway'da **1 ta replika** ishlatiladi.

### Nega long polling?

* Railway'da public domen, TLS va webhook ro'yxatdan o'tkazish shart emas — eng kam sozlash.
* Konteyner qayta ishga tushganda yangilanishlar yo'qolmaydi (Telegram ularni 24 soat saqlaydi).
* Bot faqat chiquvchi ulanishlar qiladi.

Webhook ham to'liq qo'llab-quvvatlanadi: `BOT_MODE=webhook`, `WEBHOOK_BASE_URL=https://<app>.up.railway.app`,
`WEBHOOK_SECRET=<tasodifiy satr>` (Telegram'dan kelgan so'rovlar `X-Telegram-Bot-Api-Secret-Token`
sarlavhasi bilan tekshiriladi).

### Asosiy texnik qarorlar

* **Test natijalari takrorlanuvchan:** testga kirgan har bir savolning **nusxasi (snapshot)**
  `test_questions` jadvalida saqlanadi. Bankdagi savol keyin tahrirlansa yoki o'chirilsa ham eski
  natijalar o'zgarmaydi. Bank savollari versiyalanadi (`version`).
* **Aralashtirilgan javoblar xavfsiz:** har bir urinish uchun `layout` saqlanadi: ko'rsatilgan
  harf → asl harf. Javob tekshiruvi deterministik: tanlangan ko'rsatilgan harf asl harfga
  o'giriladi va snapshotdagi `correct_option` bilan solishtiriladi. LLM javoblarni tekshirmaydi.
* **Idempotentlik:** bitta (test, xodim) uchun faqat bitta `IN_PROGRESS` urinish (partial unique
  index); bitta savolga bitta javob (unique + qatorni `FOR UPDATE` bloklash); eskirgan/ikki marta
  bosilgan tugmalar e'tiborsiz qoldiriladi; e'lon va eslatmalar atomik belgilar bilan bir marta yuboriladi.
* **Vaqt:** hamma narsa UTC'da saqlanadi va server vaqti bilan solishtiriladi; ko'rsatish
  `TIMEZONE` (standart `Asia/Tashkent`) da.
* **Statistika jadvali yo'q:** barcha statistika normallashtirilgan ma'lumotlardan hisoblanadi
  (bitta haqiqat manbai, eskirgan agregatlar yo'q).

---

## Talablar

* Python **3.12+** (3.12 va 3.13 da sinovdan o'tgan)
* PostgreSQL **14+** (tavsiya: 16 + **pgvector**). pgvector bo'lmasa avtomatik `real[]` fallback ishlaydi.
* Telegram bot tokeni (@BotFather)
* Groq API kaliti (https://console.groq.com/keys)
* Embedding uchun **alohida kalit kerak emas**: ko'p tilli model (`paraphrase-multilingual-MiniLM-L12-v2`,
  ONNX, ~220 MB) lokal ishlaydi va Docker image ichiga build paytida yuklab olinadi.

---

## Muhit o'zgaruvchilari

To'liq ro'yxat va izohlar: [`.env.example`](.env.example).

| O'zgaruvchi | Majburiy | Standart | Izoh |
|---|---|---|---|
| `BOT_TOKEN` | ✅ | — | @BotFather tokeni |
| `DATABASE_URL` | ✅ | — | `postgresql://user:pass@host:5432/db` (Railway: `${{Postgres.DATABASE_URL}}`) |
| `ADMIN_TELEGRAM_IDS` (yoki `ADMIN_IDS`) | ✅ | — | Vergul bilan: `123456789,987654321`. **Adminlik faqat shu ro'yxatdan** |
| `GEMINI_API_KEY` | ✅* | — | Google AI Studio kaliti (https://aistudio.google.com/apikey) |
| `GROQ_API_KEY` | ✅* | — | Groq kaliti. *Kamida bitta AI kaliti kerak, bo'lmasa AI generatsiya o'chadi, faqat bankdagi savollar ishlatiladi |
| `CEREBRAS_API_KEY`, `OPENROUTER_API_KEY` | | — | Qo'shimcha (ixtiyoriy) AI xizmatlari |
| `AI_PROVIDER_ORDER` | | `gemini,groq,cerebras,openrouter` | AI xizmatlari navbati: biri limitga yetsa, keyingisiga o'tiladi |
| `GEMINI_MODEL` / `GEMINI_VALIDATION_MODEL` | | `gemini-2.5-flash` / `gemini-2.5-flash-lite` | Gemini modellari (asosiy / tekshiruv) |
| `CEREBRAS_MODEL`, `OPENROUTER_MODEL` | | `gpt-oss-120b`, `openai/gpt-oss-120b:free` | Qo'shimcha xizmatlar modellari |
| `TIMEZONE` | | `Asia/Tashkent` | Ko'rsatish vaqt zonasi |
| `GROQ_MODEL` | | `openai/gpt-oss-120b` | Asosiy model (o'chirilgan bo'lsa avtomatik zaxiraga o'tadi) |
| `GROQ_FALLBACK_MODELS` | | `openai/gpt-oss-20b,qwen/qwen3-32b,llama-3.1-8b-instant` | Zaxira modellar |
| `GROQ_VALIDATION_MODEL` | | `openai/gpt-oss-20b` | Tekshiruvlar (manba va sifat) uchun kichikroq model — asosiy model limitini tejaydi |
| `AI_MIN_CONFIDENCE` | | `0.7` | Mustaqil tekshiruvchining minimal ishonchi |
| `AI_EXCERPT_MIN_SIMILARITY` | | `88` | AI iqtibosi va haqiqiy manba matni o'xshashligi (0–100) |
| `EMBEDDING_PROVIDER` | | `fastembed` | `none` — faqat PostgreSQL full-text qidiruv |
| `EMBEDDING_MODEL` / `EMBEDDING_DIM` | | MiniLM / `384` | O'lcham model bilan mos bo'lishi shart |
| `BOT_MODE` | | `polling` | `polling` yoki `webhook` |
| `WEBHOOK_BASE_URL`, `WEBHOOK_SECRET` | webhook'da | — | |
| `PORT` | | `8080` | Railway o'zi beradi; `/health` shu portda |
| `MAX_FILE_SIZE_MB` | | `20` | Telegram Bot API yuklab olish chegarasi 20 MB |
| `LOG_LEVEL`, `LOG_FORMAT` | | `INFO`, `json` | |

---

## Railway'ga joylash

Loyiha Railway uchun tayyor: `Dockerfile`, `railway.json` (healthcheck `/health`, restart siyosati,
1 replika) va `scripts/start.sh` (avval `alembic upgrade head`, keyin bot).

### 1. GitHub
```bash
git clone <repo-url> && cd sud-bot     # yoki ZIP'ni ochib: git init && git add . && git commit -m "init"
git push origin main
```

### 2. Railway loyihasi
1. https://railway.com → **New Project** → **Deploy from GitHub repo** → repozitoriyani tanlang.
   (Agar loyiha allaqachon ulangan bo'lsa — Service → **Settings → Source** da kuzatiladigan
   branch'ni tekshiring; yangi kod shu branch'ga birlashtirilganda avtomatik deploy bo'ladi.)
2. **Ma'lumotlar bazasi:** `+ New` → **Database** → **PostgreSQL**.
   *Tavsiya:* `+ New` → **Template** → **pgvector** (pgvector kengaytmasi bilan PostgreSQL).
   Oddiy PostgreSQL ham ishlaydi — migratsiya avtomatik `real[]` rejimiga o'tadi.
3. Bot servisi → **Variables**:
   ```
   BOT_TOKEN=<BotFather tokeni>
   GROQ_API_KEY=<gsk_...>
   DATABASE_URL=${{Postgres.DATABASE_URL}}      # pgvector shabloni bo'lsa: ${{pgvector.DATABASE_URL}}
   ADMIN_TELEGRAM_IDS=<sizning Telegram ID>
   TIMEZONE=Asia/Tashkent
   ```
   (Servis nomi boshqacha bo'lsa, `${{<servis-nomi>.DATABASE_URL}}` ni tanlang.)
4. Railway `railway.json` ni o'qiydi: Dockerfile bilan build qiladi, `sh scripts/start.sh` bilan
   ishga tushiradi, `/health` orqali tekshiradi. **Deploy** tugmasini bosing.
5. **Deployments → Logs** da quyidagilarni ko'rishingiz kerak:
   `Running database migrations...` → `Database ready (vector_backend=pgvector)` →
   `Bot authorised` → `Start polling`.

> **Replikalar:** polling rejimida faqat **1 ta replika** bo'lishi kerak (`railway.json` da
> `numReplicas: 1`). Deploy paytida eski va yangi konteyner qisqa vaqt bir vaqtda ishlasa,
> Telegram `Conflict` xatosini qaytaradi — aiogram avtomatik qayta urinadi. Buni butunlay
> yo'qotish uchun `RAILWAY_DEPLOYMENT_OVERLAP_SECONDS=0` o'zgaruvchisini qo'shishingiz mumkin.

> **Fon vazifalari:** alohida worker kerak emas — scheduler bot jarayoni ichida ishlaydi va
> barcha holatni PostgreSQL'da saqlaydi (konteyner qayta ishga tushsa ham ish davom etadi).

### Webhook (ixtiyoriy)
Servis → **Settings → Networking → Generate Domain**, so'ng:
```
BOT_MODE=webhook
WEBHOOK_BASE_URL=https://<sizning-domen>.up.railway.app
WEBHOOK_SECRET=<uzun tasodifiy satr>
```

---

## Telegram botni sozlash

1. @BotFather → `/newbot` → nom va username → **tokenni** `BOT_TOKEN` ga yozing.
2. @BotFather → `/setprivacy` → botni tanlang → **Disable** (bot guruhdagi xabarlarni ko'rib,
   xodimlarni guruh bilan bog'lay olishi uchun). Ixtiyoriy, lekin tavsiya etiladi.
3. Guruhda botni **administrator** qiling (tavsiya etiladi) — shunda a'zo qo'shilishi/chiqishi
   (`chat_member`) ham kuzatiladi. Guruhda botga kerakli huquqlar:
   * ✅ **Send messages** — sarlavha va savollarni yuborish;
   * ✅ **Edit messages** — test yakunida savollarga to'g'ri javob va izohni ochish (botning o'z xabarlari);
   * ➖ Delete messages — shart emas;
   * ➖ Manage polls — **talab qilinmaydi** (native poll ishlatilmaydi, custom inline keyboard).
   Guruh egasi botni oddiy a'zo qilib qo'shsa ham test ishlaydi, faqat a'zolar ro'yxatini kuzatish
   cheklanadi.
4. Buyruqlar avtomatik o'rnatiladi: `/start`, `/help`, `/cancel` (hamma uchun), `/admin` (faqat adminlar uchun).

### Birinchi admin qanday yaratiladi
1. Telegram ID'ingizni bilib oling (masalan @userinfobot ga yozing).
2. Railway → Variables → `ADMIN_TELEGRAM_IDS=<ID>` (bir nechta bo'lsa vergul bilan).
3. Botga `/start` yuboring → **👨‍💼 ADMIN PANEL** ochiladi.

Adminlik **faqat server tomonida** `ADMIN_TELEGRAM_IDS` bo'yicha tekshiriladi: `/admin` yuborish
yoki admin tugmasini soxtalashtirish hech kimga huquq bermaydi.

---

## Birinchi qadamlar

### 1) Birinchi hujjatni yuklash
`/admin` → **📚 Materiallar** → **➕ Material yuklash** → PDF/DOCX/TXT/MD faylni yuboring
(yoki matnni nusxalab yuboring) → sarlavha → toifa → tavsif (o'tkazib yuborish mumkin).
Bot holatni ko'rsatadi: `⏳ PROCESSING` → `✅ READY` (sahifalar, bo'laklar soni) yoki `❌ FAILED`
(tushunarli sabab bilan, masalan skanerlangan PDF).

Ixtiyoriy: material sahifasida **🧠 Savollar yaratish (AI)** → 5/10/15/20 — savollar bankiga
manbali savollar qo'shiladi (🟡 tasdiq kutadi).

### 2) Birinchi testni yaratish
`/admin` → **➕ Test yaratish** → nom → fayllarni yuboring → **🔀 Aralashtirib test tuzish** → son →
vaqt → kimlarga → **✅ Testni yaratish**. Bot savollarni faqat yuborilgan fayllardan tuzadi va testni
darhol tanlangan xodimlarga beradi (batafsil: «Test yaratish: bir nechta fayldan aralash test»).
Kengaytirilgan sozlash (qiyinlik, yangiliklar %, mavzu, o'tish bali, savollarni qo'lda ko'rib chiqish):
**📝 Testlar → 🛠 Bankdan test (kengaytirilgan sozlash)**.

### 3) Xodimlar qanday qo'shiladi
Xodim botga `/start` yozadi — shu zahoti bazaga qo'shiladi va **faol** bo'ladi, «📚 Testlarim»
ochiladi; admin'ga «Yangi xodim … avtomatik faollashtirildi» xabari keladi. Admin **👥 Xodimlar**
bo'limida xodimni ⛔️ bloklashi, 🗑 o'chirishi, qayta faollashtirishi mumkin. Guruhda `/register`
(faqat admin) guruhni ro'yxatga oladi — guruh testlari shu guruh a'zolariga ko'rinadi.

### 4) Statistikani ko'rish
* **📋 Natijalar** → test → xodim → har bir javob va manbasi.
* Test sahifasi → **👥 Ishtirokchilar**: jami, boshlaganlar, tugatganlar, tugatmaganlar,
  qatnashmaganlar, o'rtacha natija → xodimni bosing → uning **har bir javobi** (✅/❌, tanlagan va to'g'ri harf).
* Test sahifasi → **📊 Savollar tahlili** (har savol bo'yicha A/B/C/D taqsimoti) →
  **🧾 Kim nimani tanladi** (har savol bo'yicha har bir xodimning tanlovi).
* **📊 Statistika**, **🏆 Reyting**, **👥 Xodimlar → profil → ❌ Xato javoblar**.
* **📈 Hisobotlar** → Excel (XLSX) yoki test sahifasidan CSV.

---

## Lokal ishlab chiqish

```bash
# 1. Klonlash
git clone <repo-url> sud-bot && cd sud-bot

# 2. Virtual muhit
python3.12 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate

# 3. Bog'liqliklar
pip install -r requirements-dev.txt

# 4. .env
cp .env.example .env                  # BOT_TOKEN, GROQ_API_KEY, ADMIN_TELEGRAM_IDS ni to'ldiring

# 5. PostgreSQL (pgvector bilan)
docker compose up -d db
# .env dagi DATABASE_URL: postgresql://court:court_dev_password@localhost:5432/court_bot

# 6. Migratsiyalar
alembic upgrade head

# 7. Botni ishga tushirish
python -m app.main
```

Hammasini konteynerda ishga tushirish: `docker compose --profile bot up --build`.

---

## Ma'lumotlar bazasi va migratsiyalar

* Bazani yaratish/yangilash: `alembic upgrade head` (Railway'da har ishga tushishda avtomatik).
* Joriy versiya: `alembic current`; tarix: `alembic history`.
* Modelni o'zgartirgandan keyin yangi migratsiya:
  `alembic revision --autogenerate -m "tavsif"` → faylni tekshiring → `alembic upgrade head`.
* Sxema tekshiruvi: `alembic check` (modellar va migratsiyalar mosligi).
* Orqaga qaytarish: `alembic downgrade -1`.

**Jadvallar:** `users`, `groups`, `group_members`, `materials`, `news`, `source_chunks`
(matn bo'laklari + `embedding` + `tsvector`), `topics`, `questions` (bank, versiyalanadi),
`tests`, `test_materials`, `test_questions` (snapshot), `question_options` (snapshot variantlari),
`test_sources` (test manbalari: `source_name, original_file_name, file_id, material_id`),
`test_assignments` (tanlangan xodimlar, olib tashlanganlar, qayta topshirish ruxsati),
`test_attempts` (= «user_tests»: `status, started_at, deadline_at, completed_at, score_percent`),
`user_answers` (`attempt_id, user_id, telegram_id, test_id, test_question_id, selected_option,
selected_option_id, correct_option, is_correct, answered_at`; `UNIQUE(attempt_id, test_question_id)`;
UPDATE trigger bilan taqiqlangan), `group_test_posts`, `group_question_messages`, `bot_settings`;
`tests.duration_seconds` (shaxsiy vaqt), `tests.audience`, `tests.paused`;
`test_questions.source_id / source_name / source_file`; `users.status` = pending/active/inactive/blocked;
`material_chunks` — materiallarga tegishli bo'laklar uchun view.
Tashqi kalitlar, CHECK cheklovlari, indekslar (shu jumladan HNSW va GIN) mavjud.

**pgvector:** birinchi migratsiya `CREATE EXTENSION vector` ni sinab ko'radi. Muvaffaqiyatli bo'lsa
`embedding vector(384)` + HNSW indeks, aks holda `embedding real[]` (kosinus o'xshashlik Python'da
hisoblanadi — kichik/o'rta hajm uchun yetarli). Keyinchalik pgvector'ga o'tish uchun yangi
pgvector bazaga migratsiya qiling va `python -m scripts.reembed` ni ishga tushiring.
Embedding modelini almashtirganda ham: `python -m scripts.reembed`.

---

## AI / RAG pipeline

```
HUJJAT → MATN AJRATISH (PDF sahifa raqamlari bilan, DOCX paragraf+jadval, TXT/MD)
       → TOZALASH (unicode, takroriy sarlavha/futer, sahifa raqamlari, so'z bo'linishi)
       → BO'LAKLASH (~1200 belgi, 200 belgi overlap, paragraf/gap chegaralari, sahifa va bo'lim sarlavhasi)
       → EMBEDDING (ko'p tilli MiniLM, lokal) + PostgreSQL tsvector
       → QIDIRUV (vektor + full-text, Reciprocal Rank Fusion; kam ishlatilgan bo'laklar ustuvor)
       → KONTEKST (asosiy bo'lak + 2 ta semantik qo'shni)
       → LLM GENERATSIYA (Groq, JSON mode, qat'iy "faqat manba" prompt)
       → PYDANTIC VALIDATSIYA (noto'g'ri JSON → xato bilan qayta so'rov)
       → DETERMINISTIK TEKSHIRUV (variantlar soni, takrorlar, "barchasi to'g'ri" taqiqi,
         iqtibos haqiqiy manba matnida borligi — fuzzy ≥ 88, takroriy savollar)
       → MUSTAQIL MANBA TEKSHIRUVI (AI javob kalitini KO'RMAY, faqat manbadan javob topadi;
         javob mos kelmasa — rad etiladi)
       → SIFAT TEKSHIRUVI (bitta to'g'ri javob, noaniqlik, tushunarlilik)
       → (izoh manbaga mos bo'lmasa — manbadan qayta yoziladi)
       → MAVZU TASNIFI (mavjud mavzular ro'yxatiga moslash)
       → SAVOLLAR BANKI (🟡 tasdiq kutadi; admin ko'rib chiqadi)
```

Har bir savolda saqlanadi: manba hujjat, manba bo'lak, sahifa, manba havolasi (bo'lak
metama'lumotlaridan quriladi, AI'dan emas), iqtibos, mavzu, qiyinlik, to'g'ri javob, ichki tekshiruv
metama'lumotlari (xodimlarga ko'rsatilmaydi). Manba yetarli bo'lmasa model `rejected` qaytaradi va savol
yaratilmaydi. Promptlar: [`app/ai/prompts.py`](app/ai/prompts.py) (generatsiya, validatsiya,
izoh, manba tekshiruvi).

**Bir nechta AI xizmati (navbat bilan):**
* Har bir xizmat **o'zining bitta rasmiy kaliti** bilan ishlatiladi (bitta xizmatda bir nechta
  akkaunt/kalit orqali limitni aylanib o'tish yo'q). Navbat `AI_PROVIDER_ORDER` bo'yicha:
  Gemini → Groq → Cerebras → OpenRouter (faqat kaliti bor xizmatlar).
* Xizmat kunlik limitga yetsa, kaliti noto'g'ri bo'lsa yoki javob bermasa — keyingisiga o'tiladi;
  u limit tiklanguncha o'tkazib yuboriladi, keyin yana ishlatiladi. Hammasi tugasa, admin «N daqiqadan
  keyin» degan xabar oladi va «🔄 Davom ettirish» bilan davom ettiradi.
* Qaysi xizmat javob bermasin, savollar o'sha promptlar va o'sha 3 bosqichli tekshiruvdan o'tadi.
* Holat: **⚙️ Sozlamalar** → «🤖 AI: Google Gemini ✅ → Groq ⏳ 45 daq».
* ⚠️ Bepul tariflarda ba'zi xizmatlar (masalan, Gemini) yuborilgan matnni o'z xizmatini yaxshilashga
  ishlatishi mumkin — shaxsiy ma'lumotli fayllarni yuklamang.

**Groq limitini tejash:**
* Groq'ga faqat savol tuzishda murojaat qilinadi. Xodimlarning xabarlari, testlar va natijalar AI ishlatmaydi,
  suhbat tarixi (history) yuborilmaydi.
* Bitta bo'lakdagi savollar **bitta** ko'r manba tekshiruvi va **bitta** sifat tekshiruvi so'rovida
  tekshiriladi. Mavzu generatsiyaning o'zida (mavjud mavzular ro'yxatidan) tanlanadi. Natijada 2 savollik
  bo'lak uchun 6 o'rniga 3 so'rov ketadi: 10 savol uchun 30 o'rniga 15 so'rov, ~27% kam matn
  (`tests/test_ai_efficiency.py`). Paketdan javob kelmagan savol alohida tekshiriladi — hech biri
  tekshiruvsiz qolmaydi.
* Bir xil deterministik tekshiruv (temperature 0) 6 soat davomida keshdan olinadi (`AI_CACHE_TTL_SECONDS`).
* Fayl qayta ishlansa yoki bot qayta ishga tushsa, bankda yetarli savol bo'lsa, avto-savollar qayta tuzilmaydi.
* Bir vaqtda ko'pi bilan `GROQ_MAX_CONCURRENCY` (standart 2) so'rov ketadi. Groq javob sarlavhalarida
  (`x-ratelimit-remaining-tokens`) daqiqalik limit tugayotgani ko'rinsa, bot keyingi so'rovdan oldin limit
  tiklanishini kutadi va 429 xatosiga tushmaydi.
* 429 bo'lsa — eksponensial kutish (5s, 10s, 20s… jitter bilan, maks. 60s). Kunlik limit bo'lsa — kutib
  o'tirmay, darhol «N daqiqadan keyin» deb xabar beradi.
* Token sarfi har bir so'rov uchun logga yoziladi (`input_tokens`, `output_tokens`), har bir ish
  oxirida adminga «🔢 AI sarfi: …», kunlik jami esa **📊 Statistika** da ko'rsatiladi.
* `GROQ_API_KEY` faqat muhit o'zgaruvchisidan olinadi (`SecretStr`), loglarda yashiriladi va hech qachon
  Telegram'ga chiqarilmaydi.

**Provayder modulli:** yangi LLM qo'shish uchun `app/ai/base.py` dagi `LLMProvider` ni amalga oshiring va
`app/ai/factory.py` ga qo'shing.

---

## Avtomatik testlar

```bash
docker compose up -d db          # yoki istalgan PostgreSQL (pgvector tavsiya)
export TEST_DATABASE_URL=postgresql://court:court_dev_password@localhost:5432/court_test
pytest -q
ruff check app tests scripts
```

Testlar `court_test` bazasini **o'chirib qayta yaratadi** va Alembic migratsiyalarini ishga tushiradi.
Qamrov: ro'yxatdan o'tish, admin avtorizatsiyasi (shu jumladan soxta callback), hujjat ajratish
(PDF/DOCX/TXT/MD), bo'laklash, vektor va full-text qidiruv, generatsiya pipeline'i (to'qilgan
iqtibos va tekshiruvchi kelishmovchiligi rad etiladi), Groq provayderining HTTP yo'li (JSON mode,
429, 401), test yaratish/yig'ish, yangiliklar ulushi, ko'rib chiqish, snapshot o'zgarmasligi,
aralashtirilgan javoblar xaritasi, javob tekshiruvi, ball, idempotentlik, muddat tugashi, scheduler,
qatnashish statistikasi, xato javoblar, reyting, XLSX/CSV hisobotlar, haqiqiy aiogram Dispatcher orqali
handlerlar (soxta Telegram transport bilan), lokalizatsiya kalitlari, sirlar yo'qligi.

> Testlarda LLM o'rniga **faqat testlar uchun** skriptlangan provayder ishlatiladi (u ham manba
> matnidan haqiqiy gaplarni oladi, shuning uchun butun validatsiya zanjiri haqiqatan ishlaydi).
> Ishlab chiqarishda kaliti berilgan haqiqiy AI xizmatlari ishlatiladi.

---

## Xavfsizlik

* Barcha sirlar faqat muhit o'zgaruvchilarida; kodda kalit yo'q; `.env` `.gitignore` da.
* Sozlamalarda sirlar `SecretStr`; loglarda `BOT_TOKEN`, `GROQ_API_KEY`, `WEBHOOK_SECRET` va DB paroli
  avtomatik `***` bilan almashtiriladi.
* Admin huquqi server tomonida `ADMIN_TELEGRAM_IDS` bo'yicha (filter + har bir admin router).
* SQL faqat SQLAlchemy orqali (parametrli so'rovlar).
* Fayl turi (kengaytma + MIME) va hajmi tekshiriladi; matn hajmi cheklangan; vaqtinchalik fayllar
  ishlov berilgach o'chiriladi; Telegram `file_id` saqlanadi (vaqtinchalik `file_path` emas).
* Xodim faqat o'z natijalarini ko'radi (har bir callback'da egasi tekshiriladi).
* Konteyner root bo'lmagan foydalanuvchi bilan ishlaydi. Webhook maxfiy token bilan himoyalangan.
* Foydalanuvchi va AI matnlari Telegram HTML uchun ekranlanadi.

---

## Muammolarni hal qilish

| Belgi | Sabab / yechim |
|---|---|
| Deploy `healthcheck failed` | Loglarni ko'ring. Ko'pincha `DATABASE_URL` noto'g'ri yoki `BOT_TOKEN` yaroqsiz (`Unauthorized`). |
| `TelegramConflictError` | Ikki nusxa polling qilyapti: replikalar 1 ta bo'lsin; lokal botni o'chiring. |
| `/admin` → «faqat administratorlar uchun» | `ADMIN_TELEGRAM_IDS` da ID yo'q yoki xato; o'zgartirgandan so'ng qayta deploy. |
| Material `FAILED: PDF ichida matn topilmadi` | Skanerlangan PDF — OCR qilingan PDF yoki DOCX yuklang. |
| «AI sozlanmagan» | `GEMINI_API_KEY` yoki `GROQ_API_KEY` qo'shing. |
| Savollar kam yaratildi / «Yetishmayapti» | Manbada fakt kam yoki tekshiruv qat'iy. Ko'proq material yuklang, savollar sonini kamaytiring, test sahifasida «🧠 Savollarni yig'ish» ni qayta bosing. Sabablar statistikasi xabarda ko'rsatiladi. |
| `Groq rate limit` | Bepul tarif limiti — biroz kuting, `GROQ_VALIDATION_MODEL` ni sozlang yoki tarifni oshiring. |
| `Semantik indeks: yo'q` | Embedding modeli yuklanmadi (internet yo'q) — full-text qidiruv ishlaydi. Keyin `python -m scripts.reembed --only-missing`. |
| `vector_backend=array` logda | Bazada pgvector yo'q — ishlaydi, lekin katta hajm uchun pgvector shabloniga o'ting. |
| Guruh a'zolari aniqlanmayapti | @BotFather `/setprivacy` → Disable; botni guruh admini qiling; xodim `/start` bosganda ham tekshiriladi. |

---

## Loyiha tuzilmasi

```
app/
├── main.py                 # kirish nuqtasi: bot + scheduler + /health (+ webhook)
├── config/settings.py      # pydantic-settings (muhit o'zgaruvchilari)
├── database/               # Base, naming convention, async engine/sessiya
├── models/                 # SQLAlchemy modellari (users, materials, questions, tests, ...)
├── schemas/ai.py           # AI javoblari uchun Pydantic sxemalar
├── ai/                     # LLM interfeysi, Groq provayder, promptlar, strukturalangan chaqiruv
├── rag/                    # embeddinglar, vektor ombor (pgvector/array), retriever, DDL
├── documents/              # ajratish (PDF/DOCX/TXT/MD), tozalash, bo'laklash
├── services/               # biznes mantiq (generatsiya, bank, testlar, urinishlar, scheduler, ...)
├── statistics/             # qatnashish, xodim, reyting, dashboard
├── reports/builder.py      # XLSX / CSV
├── handlers/               # aiogram routerlari (admin/*, employee, group, common, errors)
├── keyboards/              # inline/reply klaviaturalar, callback ma'lumotlari
├── middlewares/            # DB sessiya, foydalanuvchi ro'yxati va rol
├── filters/                # IsAdmin
├── locales/uz.py           # BARCHA interfeys matnlari (ru/en qo'shish uchun shu formatda)
└── utils/                  # vaqt, matn, structured logging
migrations/                 # Alembic
scripts/                    # start.sh, preload_embeddings.py, reembed.py
tests/                      # pytest (100 test)
Dockerfile, railway.json, docker-compose.yml, requirements*.txt, .env.example
```

---

## Ma'lum cheklovlar

* **OCR yo'q:** skanerlangan (rasm) PDF'lardan matn olinmaydi — admin aniq xato xabarini oladi.
* **PDF hisobot** yo'q (XLSX va CSV mavjud).
* FSM holati (ustaning oraliq qadamlari) xotirada saqlanadi: deploy/qayta ishga tushishda tugallanmagan
  **usta** qadamlari yo'qoladi. «➕ Test yaratish»da esa qoralama test va yuklangan fayllar birinchi
  qadamdanoq bazada (test sahifasidan davom ettirish yoki o'chirish mumkin). Xodimlar, testlar,
  savollar, javoblar, deadline, progress va natijalar PostgreSQL'da — restartda yo'qolmaydi.
* Fayl o'qish va savol tuzish fonda ishlaydi; agar aynan shu paytda bot qayta ishga tushsa, jarayon
  to'xtaydi (fayl/test holati ❌ bo'lib qoladi) — materialni «🔄 Qayta ishlash», testni «🧠 Savollarni
  yig'ish» bilan davom ettiring.
* Muddatdan oldin eslatma test oynasining tugashiga qarab yuboriladi; har bir xodimning shaxsiy
  24 soatlik vaqti uchun alohida eslatma yo'q (vaqt tugaganda xabar keladi).
* Telegram Bot API fayl yuklab olish chegarasi — 20 MB.
* Xodim ko'p guruhda bo'lishi mumkin; guruh a'zoligi bot ko'rgan hodisalar asosida aniqlanadi.
* **Qayta topshirish** faqat admin ruxsati bilan (bir martalik); har bir urinish ichida bir savolga bitta javob.
* Telegram tugma matni uzun variantlarni qisqartirib ko'rsatadi — variantning to'liq matni xabarning o'zida.
