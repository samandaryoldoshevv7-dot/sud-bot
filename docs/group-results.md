# Guruh testlari

- Yangi guruh testi e'lon qilinganda tanlangan muddat boshlanadi. Masalan, 24 soatlik test ertasi kuni shu vaqtda yopiladi; kech qo'shilgan ishtirokchi umumiy yakungacha ishlaydi. Avval e'lon qilingan testlarning muddati o'zgarmaydi.
- Darhol ko'rsatish rejimida Telegram javobning to'g'ri yoki noto'g'riligini belgilaydi. «Javobim va izoh» tugmasi tanlangan javob, to'g'ri variant, sabab va manbadan parchani faqat bosgan odamga ko'rsatadi. Kechiktirilgan/yashirilgan javob rejimlari saqlanadi.
- «Mening natijam» guruhni tark etmasdan shaxsiy natijani ochadi. Tugma foydalanuvchi identifikatorini qabul qilmaydi: kim bosgani Telegram orqali tekshiriladi.
- Muddat tugagach guruhga barcha qatnashganlarning reytingi chiqadi: eng ko'p to'g'ri javob birinchi; teng ball teng o'rin. Javobsiz savollar alohida hisoblanadi.
- Shaxsiy xabarlar Telegram Bot API 10.3 ephemeral_message_parameters bilan yuboriladi. Oddiy guruh xabariga o'tuvchi zaxira yo'li yo'q. Tugma orqali barcha guruh botlari javob bera oladi; avtomatik shaxsiy bildirishnomalar uchun bot guruh administratori bo'lishi kerak.
- Vaqtinchalik shaxsiy xabar yo'qolishi mumkin, ammo javoblar va natijalar PostgreSQLda qoladi; tugmani qayta bosib ochiladi. Botni shaxsiy chatda boshlaganlar u yerda ham natija oladi.
- Yangilanish 0008 faqat yetkazish belgilarini qo'shadi. Eski savollar, javoblar va ballar o'zgarmaydi. Eski tugagan urinishlarga qayta xabar tarqatilmaydi.

Rasmiy API: https://core.telegram.org/bots/api#ephemeral-messages-and-commands
