/**
 * العربية (ar).
 *
 * Keyed by the ENGLISH SOURCE STRING, exactly as a gettext msgid: a lookup
 * that misses falls back to its own key, so a sentence changed in render.js
 * without being re-translated shows the new English rather than a fluent
 * translation of something the page no longer says.
 *
 * Entries that count something are objects keyed by the categories
 * `Intl.PluralRules` returns for this language, which is not always one and
 * other. ../i18n.js does the selecting.
 *
 * SIX PLURAL FORMS, which is the most of any language here and the reason
 * `Intl.PluralRules` does the selecting rather than a test on `n === 1`.
 * Arabic agrees differently at zero, one, two, three-to-ten, eleven-to-
 * ninety-nine, and everything else, and `one` and `two` drop the numeral
 * entirely because writing it out is what the language does there.
 * 
 * This is also the one locale the page renders under `dir="rtl"`.
 */
export default {
  "VRCVerify Status": "حالة VRCVerify",
  "Whether VRCVerify's verification, bot, invites, dashboard and website are working, and whether the services they depend on are.": "ما إذا كان التحقق والبوت والدعوات ولوحة التحكم وموقع VRCVerify تعمل، وما إذا كانت الخدمات التي تعتمد عليها تعمل.",
  "Home": "الرئيسية",
  "Dashboard": "لوحة التحكم",
  "All systems operational": "جميع الأنظمة تعمل",
  "Everything is down": "كل شيء متوقف",
  "Some services are down": "بعض الخدمات متوقفة",
  "Some services are degraded": "بعض الخدمات تعمل بأداء منخفض",
  "Service status is unknown": "حالة الخدمة غير معروفة",
  "No check has run yet": "لم يُجرَ أي فحص بعد",
  "Status cannot be read right now": "تعذّرت قراءة الحالة في الوقت الحالي",
  "Status is out of date": "الحالة قديمة",
  "No check has completed yet.": "لم يكتمل أي فحص بعد.",
  "Checked %{ago} ago, at %{time}.": "جرى الفحص قبل %{ago}، في %{time}.",
  "Operational": "يعمل",
  "Degraded": "أداء منخفض",
  "Down": "متوقف",
  "Unknown": "غير معروف",
  "Verification": "التحقق",
  "Age checks requested in Discord and answered against VRChat.": "فحوص العمر المطلوبة في Discord والمُجاب عنها عبر VRChat.",
  "Discord bot": "بوت Discord",
  "The bot responding to commands and handing out roles.": "البوت الذي يستجيب للأوامر ويمنح الأدوار.",
  "Group invites": "دعوات المجموعة",
  "Invites to a server's VRChat group after a check passes.": "الدعوات إلى مجموعة VRChat الخاصة بالخادم بعد اجتياز الفحص.",
  "Dashboard and sign-in": "لوحة التحكم وتسجيل الدخول",
  "dashboard.vrcverify.com, where servers are configured.": "dashboard.vrcverify.com، حيث تُضبط الخوادم.",
  "Website": "الموقع",
  "vrcverify.com and the documents linked from it.": "vrcverify.com والمستندات المرتبطة به.",
  "for %{duration}": "منذ %{duration}",
  "No history yet": "لا يوجد سجل بعد",
  "%{percent}% uptime over the last %{days} days, with no incidents": "%{percent}% من زمن التشغيل خلال آخر %{days} يومًا، دون أي أعطال",
  "%{day}: maintenance all day": "%{day}: صيانة طوال اليوم",
  "%{day}: no data": "%{day}: لا توجد بيانات",
  "%{day}: %{percent}% up": "%{day}: %{percent}% تشغيل",
  " (%{duration} of maintenance not counted)": " (%{duration} من الصيانة غير محتسبة)",
  "Today": "اليوم",
  "less than a minute": "أقل من دقيقة",
  "Services we depend on": "الخدمات التي نعتمد عليها",
  "Everything the bot does happens here.": "كل ما يفعله البوت يحدث هنا.",
  "Age checks are answered by VRChat's API.": "فحوص العمر تُجيب عنها واجهة VRChat البرمجية.",
  "Premium subscriptions and the billing portal.": "الاشتراكات المميزة وبوابة الفوترة.",
  "DNS, the route to the dashboard, and this page itself.": "نظام أسماء النطاقات، والمسار إلى لوحة التحكم، وهذه الصفحة نفسها.",
  "%{company}'s own status page": "صفحة حالة %{company}",
  "Read from each company's own status feed. VRCVerify cannot fix these, and when one of them is down our rows will usually follow.": "تُقرأ من موجز الحالة الخاص بكل شركة. لا يستطيع VRCVerify إصلاحها، وعندما تتوقف إحداها فإن صفوفنا تتبعها عادةً.",
  "Recent incidents": "الأعطال الأخيرة",
  "Ongoing for %{duration}. Started %{time}.": "مستمر منذ %{duration}. بدأ %{time}.",
  "Resolved": "تم الحل",
  "Resolved after %{duration}. Started %{time}.": "تم الحل بعد %{duration}. بدأ %{time}.",
  "Investigating": "قيد التحقيق",
  "Identified": "تم تحديد السبب",
  "Monitoring": "قيد المراقبة",
  "Nothing has gone wrong in the last %{days} days. Anything that does is written up here, and stays.": "لم يحدث أي خلل خلال آخر %{days} يومًا. وكل ما يحدث يُدوَّن هنا ويبقى.",
  "The checker last reported more than five minutes ago, so every row above is shown as unknown rather than as whatever it said last. The services themselves may well be fine.": "أبلغ الفاحص آخر مرة قبل أكثر من خمس دقائق، لذا يظهر كل صف أعلاه على أنه غير معروف بدلًا من آخر ما قاله. وقد تكون الخدمات نفسها بخير تمامًا.",
  "This page cannot reach its own storage, so it has nothing to report. That is a fault in the status page and says nothing about whether the services are working.": "لا تستطيع هذه الصفحة الوصول إلى مخزنها الخاص، فليس لديها ما تُبلغ عنه. هذا خلل في صفحة الحالة نفسها ولا يقول شيئًا عمّا إذا كانت الخدمات تعمل.",
  "No check has completed yet. This is what the page looks like before its first run, and it should correct itself within a minute.": "لم يكتمل أي فحص بعد. هكذا تبدو الصفحة قبل تشغيلها الأول، ومن المفترض أن تصحّح نفسها خلال دقيقة.",
  "Times are UTC. Everything is checked once a minute, and a problem has to show up twice in a row before it is published here, so a fault takes about two minutes to appear. Verification, the Discord bot and group invites report in on their own schedule rather than being reached directly, which can take about four. Recoveries are published as soon as they are seen. This page runs on Cloudflare, separately from everything it reports on, so that it stays up when they do not. Machine readable: %{link}.": "الأوقات بتوقيت UTC. يُفحص كل شيء مرة واحدة كل دقيقة، ويجب أن تظهر المشكلة مرتين متتاليتين قبل نشرها هنا، لذا يستغرق ظهور أي عطل نحو دقيقتين. أما التحقق وبوت Discord ودعوات المجموعة فتُبلغ وفق جدولها الخاص بدلًا من الاتصال بها مباشرةً، وقد يستغرق ذلك نحو أربع دقائق. ويُنشر التعافي فور رصده. تعمل هذه الصفحة على Cloudflare، منفصلةً عن كل ما تُبلغ عنه، لكي تبقى متاحة حين لا تكون تلك الخدمات كذلك. قابل للقراءة آليًا: %{link}.",
  "What's new": "ما الجديد",
  "Terms of Service": "شروط الخدمة",
  "Privacy Policy": "سياسة الخصوصية",
  "Refund Policy": "سياسة الاسترداد",
  "Contact": "اتصل بنا",
  "VRCVerify is operated by Esatto Technologies, United States.": "يُشغّل VRCVerify شركة Esatto Technologies، الولايات المتحدة.",
  "Not affiliated with, endorsed by, or sponsored by VRChat Inc. or Discord Inc.": "غير تابع لشركة VRChat Inc. أو Discord Inc.، ولا معتمد أو مدعوم منهما.",

  "%{count} day": { zero: "%{count} يوم", one: "يوم واحد", two: "يومان", few: "%{count} أيام", many: "%{count} يومًا", other: "%{count} يوم" },
  "%{count} hour": { zero: "%{count} ساعة", one: "ساعة واحدة", two: "ساعتان", few: "%{count} ساعات", many: "%{count} ساعة", other: "%{count} ساعة" },
  "%{count} minute": { zero: "%{count} دقيقة", one: "دقيقة واحدة", two: "دقيقتان", few: "%{count} دقائق", many: "%{count} دقيقة", other: "%{count} دقيقة" },
  "%{count} day ago": { zero: "قبل %{count} يوم", one: "قبل يوم واحد", two: "قبل يومين", few: "قبل %{count} أيام", many: "قبل %{count} يومًا", other: "قبل %{count} يوم" },
  "%{count} open incident": { zero: "%{count} عطل مفتوح", one: "عطل واحد مفتوح", two: "عطلان مفتوحان", few: "%{count} أعطال مفتوحة", many: "%{count} عطلًا مفتوحًا", other: "%{count} عطل مفتوح" },
  "%{count} update": { zero: "%{count} تحديث", one: "تحديث واحد", two: "تحديثان", few: "%{count} تحديثات", many: "%{count} تحديثًا", other: "%{count} تحديث" },
  "%{count} earlier update": { zero: "%{count} تحديث سابق", one: "تحديث سابق واحد", two: "تحديثان سابقان", few: "%{count} تحديثات سابقة", many: "%{count} تحديثًا سابقًا", other: "%{count} تحديث سابق" },
  "%{percent}% uptime over the last %{days} days, with %{count} day affected": { zero: "%{percent}% من زمن التشغيل خلال آخر %{days} يومًا، مع تأثر %{count} يوم", one: "%{percent}% من زمن التشغيل خلال آخر %{days} يومًا، مع تأثر يوم واحد", two: "%{percent}% من زمن التشغيل خلال آخر %{days} يومًا، مع تأثر يومين", few: "%{percent}% من زمن التشغيل خلال آخر %{days} يومًا، مع تأثر %{count} أيام", many: "%{percent}% من زمن التشغيل خلال آخر %{days} يومًا، مع تأثر %{count} يومًا", other: "%{percent}% من زمن التشغيل خلال آخر %{days} يومًا، مع تأثر %{count} يوم" },
};
