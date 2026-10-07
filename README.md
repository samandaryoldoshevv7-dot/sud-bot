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
| 👥 Xodimlar | ro'yxat (sahifalash), qidirish, tasdiqlash/faollashtirish/o'chirish, profil, statistika, zaif/kuchli mavzular, **xato javoblar** tarixi |
| 📚 Materiallar | PDF / DOCX / TXT / MD fayl yoki nusxalangan matn yuklash; avtomatik ajratish → tozalash → bo'laklash → embedding; holat UPLOADED/PROCESSING/READY/FAILED; xato sababi; AI bilan savol yaratish; arxivlash/o'chirish |
| 📰 Yangiliklar | sarlavha, matn, sana, manba, toifa, faol/nofaol; testlarda manba sifatida (masalan 30%) |
| 📝 Testlar | 17 qadamli usta: nom, tavsif, savollar soni, variantlar (3–5), qiyinlik, manbalar, yangiliklar %, mavzu, auditoriya (hamma yoki guruh), boshlanish, muddat, aralashtirish, javoblarni ko'rsatish rejimi, qayta topshirish, o'tish bali. Hayot sikli: **DRAFT → READY → ACTIVE → EXPIRED / CLOSED** |
| 👀 Ko'rib chiqish | har bir savol: ✅ Tasdiqlash / ❌ Rad etish / 🔄 Qayta yaratish; manba va iqtibos ko'rinadi. Tasdiqlanmagan savollar bilan test e'lon qilinmaydi |
| 🗂 Savollar banki | qayta ishlatiladigan bank: filtr (holat, mavzu, material, yangilik, qiyinlik), qidirish, tasdiqlash, tahrirlash (versiyalanadi), o'chirish, qayta yaratish, izohni manbadan AI bilan qayta yozish |
| 👥 Ishtirokchilar | har bir test uchun: **Jami / Yakunlagan / Jarayonda / Muddati o'tgan / Bekor / Qatnashmagan**; ball, to'g'ri, xato, vaqt, boshlangan va tugagan vaqt; urinishni bekor qilish (qayta topshirishga ruxsat) |
| 📊 Statistika | xodimlar, testlar, urinishlar, o'rtacha ball, qatnashish darajasi, eng qiyin savollar, eng ko'p xatolar, zaif mavzular |
| 🏆 Reyting | kunlik / haftalik / oylik / umumiy (yetarli ma'lumoti yo'q xodimlar reytingga kirmaydi — sozlanadi) |
| 📈 Hisobotlar | XLSX (test bo'yicha: umumiy, ishtirokchilar, qatnashmaganlar, urinishlar, xato javoblar, savollar tahlili; umumiy: natijalar + xodimlar bo'yicha xulosa + testlar) va CSV |
| 👥 Guruhlar | Telegram guruhlarni ro'yxatga olish, a'zolar, test e'lonini guruhga yuborish |
| ⚙️ Sozlamalar | avto-tasdiqlash, xabarnomalar, eslatma vaqti, reyting minimumi va h.k. |

**Xodim:** `📝 Mening testlarim`, `📊 Natijalarim`, `ℹ️ Yordam`. Test kartasi → **▶️ TESTNI BOSHLASH** →
har bir savol alohida xabar, A/B/C/D tugmalari → yakunda natija (to'g'ri, xato, foiz, vaqt) va
(ruxsat bo'lsa) xatolar tahlili. Xodim faqat **o'z** natijalarini ko'radi.

**Avtomatik (scheduler):** rejalashtirilgan testni vaqtida faollashtirish va e'lon qilish, muddat
tugaganda testni va tugallanmagan urinishlarni yopish, muddatdan oldin eslatma, test tugaganda
adminlarga yakuniy hisobot.

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
| `ADMIN_TELEGRAM_IDS` | ✅ | — | Vergul bilan: `123456789,987654321`. **Adminlik faqat shu ro'yxatdan** |
| `GROQ_API_KEY` | ✅* | — | *Bo'lmasa AI generatsiya o'chadi, faqat bankdagi tasdiqlangan savollar ishlatiladi |
| `TIMEZONE` | | `Asia/Tashkent` | Ko'rsatish vaqt zonasi |
| `GROQ_MODEL` | | `llama-3.3-70b-versatile` | Asosiy model |
| `GROQ_VALIDATION_MODEL` | | (= `GROQ_MODEL`) | Tekshiruv chaqiruvlari uchun alohida model |
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
3. Guruhda botni **administrator** qiling — shunda a'zo qo'shilishi/chiqishi (`chat_member`) ham kuzatiladi.
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
`/admin` → **📝 Testlar** → **➕ Yangi test yaratish** → ustaning savollariga javob bering →
**✅ Testni yaratish**. Bot savollarni avval **bankdan**, yetmasa **AI yordamida faqat tanlangan
manbalardan** yig'adi.
Keyin: **👀 Savollarni ko'rish** → har birini ✅ / ❌ / 🔄 → **✅ Tayyor (READY)** → **📢 E'lon qilish**.
E'lon: ro'yxatdagi guruhlarga **▶️ TESTNI BOSHLASH** havolasi va xodimlarga shaxsiy xabar.

### 3) Xodimlar qanday qo'shiladi
* **Guruh orqali (tavsiya):** botni ish guruhiga qo'shing va guruhda `/register` yuboring (faqat admin).
  Xodim guruhdagi tugma yoki `/start` orqali botni ochadi → F.I.Sh. kiritadi → guruh a'zosi
  bo'lgani tekshiriladi → **avtomatik tasdiqlanadi** (sozlamalarda o'chirish mumkin).
* **To'g'ridan-to'g'ri:** xodim botga `/start` yozadi → F.I.Sh. → admin'ga
  «🆕 Yangi xodim» xabari keladi → **✅ Tasdiqlash**.

### 4) Statistikani ko'rish
* Test sahifasi → **👥 Ishtirokchilar** (filtrlar: yakunlagan / jarayonda / muddati o'tgan /
  qatnashmagan) → xodimni bosing → xato javoblari.
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
`tests`, `test_materials`, `test_questions` (snapshot), `test_attempts`, `user_answers`, `bot_settings`.
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
izoh, mavzu tasnifi, manba tekshiruvi).

**Groq limitlari:** bepul tarifda daqiqalik token limiti past. Provayder 429 javoblarida
`retry-after` bo'yicha kutib qayta urinadi. Ko'p savol kerak bo'lsa: `GROQ_VALIDATION_MODEL=llama-3.1-8b-instant`
(tekshiruvlar uchun alohida limit) yoki pullik tarif.

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
> Ishlab chiqarishda faqat Groq ishlatiladi.

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
| «AI sozlanmagan» | `GROQ_API_KEY` qo'shing. |
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
tests/                      # pytest (76 test)
Dockerfile, railway.json, docker-compose.yml, requirements*.txt, .env.example
```

---

## Ma'lum cheklovlar

* **OCR yo'q:** skanerlangan (rasm) PDF'lardan matn olinmaydi — admin aniq xato xabarini oladi.
* **PDF hisobot** yo'q (XLSX va CSV mavjud).
* FSM holati (masalan, test yaratish ustasining oraliq qadamlari) xotirada saqlanadi: deploy/qayta
  ishga tushishda tugallanmagan **usta** qadamlari yo'qoladi. Test urinishlari, javoblar va barcha
  ma'lumotlar PostgreSQL'da — ular yo'qolmaydi, xodim testni davom ettira oladi.
* Telegram Bot API fayl yuklab olish chegarasi — 20 MB.
* Xodim ko'p guruhda bo'lishi mumkin; guruh a'zoligi bot ko'rgan hodisalar asosida aniqlanadi.
