# locales.py

# -- list of supported language codes --
LANGUAGE_CODES = [
    "en-US", "es-ES", "zh-CN", "ja", "de", "nl",
    "hi-IN", "ar", "bn", "pt-BR", "ru", "pa-IN",
]

# -- actual localized strings --
localizations: dict[str, dict[str, str]] = {
    "en-US": {
        "not_verified":               "⚠️ You haven't verified yet. Please click **Begin Verification** first.",
        "already_verified":           "✅ You're already verified! Role assigned (or re-assigned).",
        "recheck_started":            "🔎 We're re-checking your VRChat 18+ status. If you've updated your VRChat age verification, you'll get a DM soon!",
        "dm_role_success":            "✅ You've been verified and given **{role}** in **{server}**!",
        "nickname_update_requested":  "🔄 Nickname update requested. I'll DM you once it's done!",
        "verification_requested":     "🔎 Verification request sent! We'll DM you once we finish checking your VRChat profile.",
        "setup_missing":             "⚠️ This server hasn't set up a verification role yet. Please contact an admin.",
        "not_18_plus":               "❌ You are not 18+ according to VRChat. Contact an admin if this is an error.",
        "support_info":              "Need help with verification?\n- Contact a server admin for assistance\n- Or visit our support page at https://esattotech.com/contact-us/\n\nIf this is an error, please let us know!",
        "subscription_info":         "I've decided to offer this free of charge however if you wish to still support me, you can find my Ko-fi here:{kofi_link}. Thank you for your continued support ♥",
        "settings_saved": "✅ Settings saved!",
    # Newly added keys
    "invalid_vrc_id_input":      "❌ It looks like you entered your display name instead of your VRChat userID.\nPlease enter either the full profile URL or your userID (which always starts with `usr_`).\nhttps://imgur.com/a/EEl6ekH",
    "code_not_found":            "❌ We couldn’t find your code in your VRChat bio. Please try again.",
    "nickname_updated":          "🔔 Your nickname was updated to {display_name}.",
    "nickname_update_failed":    "⚠️ We could not update your nickname.",
    "setup_success":             "✅ Successfully {action} server config.\nVerified Role set to: `{role}` (ID={role_id})",
    "setup_unverified_set":      "\nUnverified Role to remove: `{role}` (ID={role_id})",
    "setup_unverified_missing":  "\n(Unverified role not set; no role will be removed on verification.)",
        "instructions_title": "How to Use the VRChat Verification Bot",
        "instructions_desc": "**Follow these steps** to verify your 18+ status:\n\n" +
                              "1. Click the **Begin Verification** button (if shown) or type `/vrcverify` anywhere.\n" +
                              "2. If you're new, you'll be asked for your VRChat username\n" +
                              "3. The bot will give you a unique code - put this in your VRChat bio\n" +
                              "4. Press **Verify** in Discord once your bio is updated\n\n" +
                              "If you need additional help, contact an admin or type `/vrcverify_support`.",
        "btn_begin_verification": "Begin Verification",
        "btn_update_nickname": "Update Nickname",
        "settings_intro": "⚙️ **VRChat Verify Settings**\n\n1.) **Enable auto nickname change**\n   Automatically update users' Discord nicknames to match their VRChat display names.\n   Current: **{current}**",
    },

    "es-ES": {
        "not_verified":               "⚠️ Aún no estás verificado. Por favor haz clic en **Iniciar Verificación** primero.",
        "already_verified":           "✅ ¡Ya estás verificado! Rol asignado (o reasignado).",
        "recheck_started":            "🔎 Estamos revisando de nuevo tu estado 18+. Si has actualizado tu verificación de edad, ¡recibirás un DM pronto!",
        "dm_role_success":            "✅ ¡Has sido verificado y se te ha asignado **{role}** en **{server}**!",
        "nickname_update_requested":  "🔄 Solicitud de actualización de apodo enviada. ¡Te enviaré un DM cuando esté listo!",
        "verification_requested":     "🔎 Solicitud de verificación enviada. ¡Te enviaré un DM cuando terminemos de verificar tu perfil de VRChat!",
        "setup_missing":             "⚠️ Este servidor aún no ha configurado un rol de verificación. Por favor, contacta a un administrador.",
        "not_18_plus":               "❌ No tienes 18+ según VRChat. Contacta a un administrador si esto es un error.",
        "support_info":              "¿Necesitas ayuda con la verificación?\n- Contacta a un administrador del servidor para asistencia\n- O visita nuestra página de soporte en https://esattotech.com/contact-us/\n\n¡Si esto es un error, háznoslo saber!",
        "subscription_info":         "He decidido ofrecer esto de forma gratuita, pero si deseas apoyarme, puedes encontrar mi Ko-fi aquí:{kofi_link}. ¡Gracias por tu continuo apoyo ♥",
        "settings_saved": "✅ Configuración guardada!",
    "invalid_vrc_id_input":      "❌ Parece que ingresaste tu nombre para mostrar en lugar de tu ID de usuario de VRChat.\nPor favor, ingresa la URL completa del perfil o tu ID de usuario (que siempre comienza con `usr_`).\nhttps://imgur.com/a/EEl6ekH",
    "code_not_found":            "❌ No pudimos encontrar tu código en tu biografía de VRChat. Inténtalo de nuevo.",
    "nickname_updated":          "🔔 Tu apodo fue actualizado a {display_name}.",
    "nickname_update_failed":    "⚠️ No pudimos actualizar tu apodo.",
    "setup_success":             "✅ Configuración del servidor {action}.\nRol verificado establecido en: `{role}` (ID={role_id})",
    "setup_unverified_set":      "\nRol no verificado a eliminar: `{role}` (ID={role_id})",
    "setup_unverified_missing":  "\n(Rol no verificado no configurado; no se eliminará ningún rol al verificar.)",
        "instructions_title": "Cómo usar el bot de verificación de VRChat",
        "instructions_desc": "**Sigue estos pasos** para verificar tu estado 18+:\n\n" +
                              "1. Haz clic en el botón **Iniciar Verificación** (si se muestra) o escribe `/vrcverify` en cualquier lugar.\n" +
                              "2. Si eres nuevo, se te pedirá tu nombre de usuario de VRChat\n" +
                              "3. El bot te dará un código único: colócalo en tu biografía de VRChat\n" +
                              "4. Presiona **Verificar** en Discord una vez que tu biografía esté actualizada\n\n" +
                              "Si necesitas ayuda adicional, contacta a un admin o escribe `/vrcverify_support`.",
        "btn_begin_verification": "Iniciar Verificación",
        "btn_update_nickname": "Actualizar Apodo",
        "settings_intro": "⚙️ **Configuración de Verificación VRChat**\n\n1.) **Habilitar cambio automático de apodo**\n   Actualiza automáticamente los apodos de Discord de los usuarios para que coincidan con sus nombres de pantalla de VRChat.\n   Actual: **{current}**",
    },

    "zh-CN": {
        "not_verified":               "⚠️ 您尚未通过验证。请先点击 **开始验证**。",
        "already_verified":           "✅ 您已通过验证! 角色已分配（或重新分配）。",
        "recheck_started":            "🔎 我们正在重新检查您的 18+ 状态。如果您已更新 VRChat 年龄验证,很快就会收到私信! ",
        "dm_role_success":            "✅ 您已验证并获得 **{role}** 角色,位于 **{server}**!",
        "nickname_update_requested":  "🔄 已请求更新昵称。完成后我会通过私信通知您! ",
        "verification_requested":     "🔎 验证请求已发送! 完成检查后,我会私信通知您! ",
        "setup_missing":             "⚠️ 此服务器尚未设置验证角色。请联系管理员。",
        "not_18_plus":               "❌ 根据VRChat,您未满18岁。如果有误,请联系管理员。",
        "support_info":              "需要验证帮助？\n- 联系服务器管理员获取帮助\n- 或访问我们的支持页面:https://esattotech.com/contact-us/\n\n如果有误,请告诉我们! ",
        "subscription_info":         "我决定免费提供此功能,如果您愿意支持我,可以点击此处的我的Ko-fi:{kofi_link}。感谢您的持续支持! ",
        "settings_saved": "✅ 设置已保存!",
    "invalid_vrc_id_input":      "❌ 您似乎输入了显示名称而不是 VRChat 用户ID。\n请输入完整的个人资料 URL 或以 `usr_` 开头的用户ID。\nhttps://imgur.com/a/EEl6ekH",
    "code_not_found":            "❌ 我们未能在您的 VRChat 简介中找到该代码。请重试。",
    "nickname_updated":          "🔔 您的昵称已更新为 {display_name}。",
    "nickname_update_failed":    "⚠️ 我们无法更新您的昵称。",
    "setup_success":             "✅ 成功{action} 服务器配置。\n已将已验证角色设置为: `{role}` (ID={role_id})",
    "setup_unverified_set":      "\n待移除的未验证角色: `{role}` (ID={role_id})",
    "setup_unverified_missing":  "\n（未设置未验证角色；验证后不会移除任何角色。）",
        "instructions_title": "如何使用 VRChat 验证机器人",
        "instructions_desc": "**按照这些步骤** 验证您的 18+ 状态:\n\n" +
                              "1. 点击 **开始验证** 按钮（如果显示）或在任何地方输入 `/vrcverify`。\n" +
                              "2. 如果您是新用户,系统会要求您提供 VRChat 用户名\n" +
                              "3. 机器人会给您一个唯一的代码 - 将其放入您的 VRChat 个人资料中\n" +
                              "4. 更新个人资料后,在 Discord 中按 **验证**\n\n" +
                              "如果您需要额外的帮助,请联系管理员或输入 `/vrcverify_support`。",
        "btn_begin_verification": "开始验证",
        "btn_update_nickname": "更新昵称",
        "settings_intro": "⚙️ **VRChat 验证设置**\n\n1.) **启用自动昵称更改**\n   自动更新用户的 Discord 昵称以匹配他们的 VRChat 显示名称。\n   当前:**{current}**",
    },

    "ja": {
        "not_verified":               "⚠️ まだ認証されていません。最初に **認証を開始** をクリックしてください。",
        "already_verified":           "✅ 既に認証済みです! ロールが適用されました（または再適用されました）。",
        "recheck_started":            "🔎 VRChat の 18+ ステータスを再チェックしています。年齢認証を更新している場合は、すぐにDMが届きます!",
        "dm_role_success":            "✅ 認証され、**{server}** の **{role}** ロールが付与されました! ",
        "nickname_update_requested":  "🔄 ニックネーム更新をリクエストしました。完了したらDMでお知らせします!",
        "verification_requested":     "🔎 検証リクエストを送信しました! 完了後にDMでお知らせします! ",
        "setup_missing":             "⚠️ このサーバーはまだ検証ロールを設定していません。管理者に連絡してください。",
        "not_18_plus":               "❌ VRChatによると18歳以上ではありません。エラーの場合は管理者にお問い合わせください。",
        "support_info":              "検証に関するサポートが必要ですか？\n- サーバー管理者にお問い合わせください\n- サポートページ:https://esattotech.com/contact-us/\n\nエラーの場合はお知らせください! ",
        "subscription_info":         "この機能は無料で提供していますが、サポートしていただける場合はKo-fiをご覧ください:{kofi_link}。継続的なサポートに感謝します! ",
        "settings_saved": "✅ 設定が保存されました!",
    "invalid_vrc_id_input":      "❌ 表示名ではなく VRChat のユーザーID を入力してください。\nプロフィール URL または `usr_` で始まるユーザーID を入力してください。\nhttps://imgur.com/a/EEl6ekH",
    "code_not_found":            "❌ VRChat の自己紹介にコードが見つかりませんでした。もう一度お試しください。",
    "nickname_updated":          "🔔 ニックネームを {display_name} に更新しました。",
    "nickname_update_failed":    "⚠️ ニックネームを更新できませんでした。",
    "setup_success":             "✅ サーバー設定を{action}しました。\n認証済みロール: `{role}` (ID={role_id})",
    "setup_unverified_set":      "\n削除する未認証ロール: `{role}` (ID={role_id})",
    "setup_unverified_missing":  "\n（未認証ロールは未設定です。認証時にロールは削除されません。）",
        "instructions_title": "VRChat 認証ボットの使い方",
        "instructions_desc": "**この手順に従って** 18 歳以上であることを確認してください:\n\n" +
                              "1. **認証を開始** ボタンをクリックするか、任意の場所に `/vrcverify` と入力します。\n" +
                              "2. 初めての場合は、VRChat ユーザー名を求められます。\n" +
                              "3. ボットが一意のコードを提供します。このコードを VRChat の自己紹介に入力します。\n" +
                              "4. 自己紹介を更新したら、Discord で **確認** を押します。\n\n" +
                              "追加のヘルプが必要な場合は、管理者に連絡するか、`/vrcverify_support` と入力してください。",
        "btn_begin_verification": "認証を開始",
        "btn_update_nickname": "ニックネームを更新",
        "settings_intro": "⚙️ **VRChat 認証設定**\n\n1.) **自動ニックネーム変更を有効にする**\n   ユーザーの Discord ニックネームを VRChat の表示名に合わせて自動的に更新します。\n   現在: **{current}**",
    },

    "de": {
        "not_verified":               "⚠️ Du bist noch nicht verifiziert. Bitte klicke zuerst auf **Verifizierung starten**.",
        "already_verified":           "✅ Du bist bereits verifiziert! Rolle zugewiesen (oder erneut zugewiesen).",
        "recheck_started":            "🔎 Wir prüfen deinen 18+ Status erneut. Wenn du deine Altersverifizierung aktualisiert hast, erhältst du bald eine DM!",
        "dm_role_success":            "✅ Du wurdest verifiziert und hast die Rolle **{role}** auf **{server}** erhalten!",
        "nickname_update_requested":  "🔄 Anfrage zur Spitznamenaktualisierung gesendet. Ich werde dich per DM benachrichtigen, sobald es fertig ist!",
        "verification_requested":     "🔎 Verifizierungsanfrage gesendet! Ich sende dir eine DM, sobald die Überprüfung abgeschlossen ist!",
        "setup_missing":             "⚠️ Dieser Server hat noch keine Verifizierungsrolle eingerichtet. Bitte kontaktiere einen Administrator.",
        "not_18_plus":               "❌ Laut VRChat bist du nicht 18+. Kontaktiere einen Administrator, wenn dies ein Fehler ist.",
        "support_info":              "Brauchst du Hilfe bei der Verifizierung?\n- Kontaktiere einen Server-Administrator\n- Oder besuche unsere Support-Seite: https://esattotech.com/contact-us/\n\nWenn dies ein Fehler ist, lass es uns wissen!",
        "subscription_info":         "Ich biete dies kostenlos an, aber wenn du mich unterstützen möchtest, findest du meinen Ko-fi hier: {kofi_link}. Danke für deine Unterstützung!",
        "settings_saved": "✅ Einstellungen gespeichert!",
    "invalid_vrc_id_input":      "❌ Du hast offenbar deinen Anzeigenamen statt deiner VRChat-Benutzer-ID eingegeben.\nBitte gib die vollständige Profil-URL oder deine Benutzer-ID (beginnt immer mit `usr_`) ein.\nhttps://imgur.com/a/EEl6ekH",
    "code_not_found":            "❌ Wir konnten deinen Code nicht in deiner VRChat-Biografie finden. Bitte versuche es erneut.",
    "nickname_updated":          "🔔 Dein Spitzname wurde auf {display_name} aktualisiert.",
    "nickname_update_failed":    "⚠️ Wir konnten deinen Spitznamen nicht aktualisieren.",
    "setup_success":             "✅ Serverkonfiguration {action}.\nVerifizierte Rolle festgelegt auf: `{role}` (ID={role_id})",
    "setup_unverified_set":      "\nZu entfernende unverifizierte Rolle: `{role}` (ID={role_id})",
    "setup_unverified_missing":  "\n(Unverifizierte Rolle nicht gesetzt; es wird keine Rolle bei Verifizierung entfernt.)",
        "instructions_title": "So verwenden Sie den VRChat-Verifizierungsbot",
        "instructions_desc": "**Befolgen Sie diese Schritte**, um Ihren 18+-Status zu überprüfen:\n\n" +
                              "1. Klicken Sie auf die Schaltfläche **Verifizierung starten** (falls angezeigt) oder geben Sie `/vrcverify` überall ein.\n" +
                              "2. Wenn Sie neu sind, werden Sie nach Ihrem VRChat-Benutzernamen gefragt.\n" +
                              "3. Der Bot gibt Ihnen einen eindeutigen Code - fügen Sie diesen in Ihre VRChat- Biografie ein.\n" +
                              "4. Drücken Sie **Überprüfen** in Discord, sobald Ihre Biografie aktualisiert wurde.\n\n" +
                              "Wenn Sie zusätzliche Hilfe benötigen, wenden Sie sich an einen Administrator oder geben Sie `/vrcverify_support` ein.",
        "btn_begin_verification": "Verifizierung starten",
        "btn_update_nickname": "Spitznamen aktualisieren",
        "settings_intro": "⚙️ **VRChat-Verifizierungseinstellungen**\n\n1.) **Automatische Änderung des Spitznamens aktivieren**\n   Aktualisieren Sie automatisch die Discord-Spitznamen der Benutzer, um mit ihren VRChat-Anzeigenamen übereinzustimmen.\n   Aktuell: **{current}**",
    },

    "nl": {
        "not_verified":               "⚠️ Je bent nog niet geverifieerd. Klik eerst op **Verificatie starten**.",
        "already_verified":           "✅ Je bent al geverifieerd! Rol toegewezen (of opnieuw toegewezen).",
        "recheck_started":            "🔎 We controleren je 18+ status opnieuw. Als je je leeftijdsverificatie hebt bijgewerkt, ontvang je binnenkort een DM!",
        "dm_role_success":            "✅ Je bent geverifieerd en hebt de rol **{role}** in **{server}** ontvangen!",
        "nickname_update_requested":  "🔄 Verzoek om bijnaam bij te werken verstuurd. Ik stuur je een DM zodra het klaar is!",
        "verification_requested":     "🔎 Verificatieverzoek verzonden! Ik stuur je een DM zodra de controle is voltooid!",
        "setup_missing":             "⚠️ Deze server heeft nog geen verificatierol ingesteld. Neem contact op met een beheerder.",
        "not_18_plus":               "❌ Volgens VRChat ben je niet 18+. Neem contact op met een beheerder als dit een fout is.",
        "support_info":              "Heb je hulp nodig bij verificatie?\n- Neem contact op met een serverbeheerder\n- Of bezoek onze ondersteuningspagina: https://esattotech.com/contact-us/\n\nLaat ons weten als dit een fout is!",
        "subscription_info":         "Ik bied dit gratis aan, maar als je me wilt ondersteunen, vind je mijn Ko-fi hier: {kofi_link}. Dank je voor je steun!",
    "settings_saved": "✅ Instellingen opgeslagen!",
    "invalid_vrc_id_input":      "❌ Het lijkt erop dat je je weergavenaam hebt ingevoerd in plaats van je VRChat userID.\nVoer de volledige profiel-URL in of je userID (begint altijd met `usr_`).\nhttps://imgur.com/a/EEl6ekH",
    "code_not_found":            "❌ We konden je code niet vinden in je VRChat-bio. Probeer het opnieuw.",
    "nickname_updated":          "🔔 Je bijnaam is bijgewerkt naar {display_name}.",
    "nickname_update_failed":    "⚠️ We konden je bijnaam niet bijwerken.",
    "setup_success":             "✅ Serverconfiguratie {action}.\nGeverifieerde rol ingesteld op: `{role}` (ID={role_id})",
    "setup_unverified_set":      "\nTe verwijderen niet-geverifieerde rol: `{role}` (ID={role_id})",
    "setup_unverified_missing":  "\n(Niet-geverifieerde rol niet ingesteld; er wordt geen rol verwijderd bij verificatie.)",
        "instructions_title": "Hoe de VRChat-verificatiebot te gebruiken",
        "instructions_desc": "**Volg deze stappen** om je 18+ status te verifiëren:\n\n" +
                              "1. Klik op de knop **Verificatie starten** (indien weergegeven) of typ `/vrcverify` ergens.\n" +
                              "2. Als je nieuw bent, word je gevraagd naar je VRChat-gebruikersnaam\n" +
                              "3. De bot geeft je een unieke code - zet deze in je VRChat-bio\n" +
                              "4. Druk op **Verifiëren** in Discord zodra je bio is bijgewerkt\n\n" +
                              "Als je extra hulp nodig hebt, neem dan contact op met een beheerder of typ `/vrcverify_support`.",
        "btn_begin_verification": "Verificatie starten",
        "btn_update_nickname": "Bijnaam bijwerken",
        "settings_intro": "⚙️ **VRChat Verificatie-instellingen**\n\n1.) **Automatische bijnaamwijziging inschakelen**\n   Werk automatisch de Discord-bijlenamen van gebruikers bij om overeen te komen met hun VRChat-weergavenamen.\n   Huidig: **{current}**",
    },

    "hi-IN": {
        "not_verified":               "⚠️ आप अभी तक सत्यापित नहीं हैं। कृपया पहले **पुष्टि शुरू करें** पर क्लिक करें।",
        "already_verified":           "✅ आप पहले से ही सत्यापित हैं! रोल सौंपा गया है (या पुनः सौपा गया)।",
        "recheck_started":            "🔎 हम आपके VRChat 18+ स्थिति की पुनः जाँच कर रहे हैं। यदि आपने अपना आयु सत्यापन अपडेट किया है, तो आपको जल्द ही एक डीएम मिलेगा!",
        "dm_role_success":            "✅ आप सत्यापित हैं और आपको **{role}** भूमिका **{server}** में प्रदान की गई है!",
        "nickname_update_requested":  "🔄 उपनाम अपडेट का अनुरोध भेजा गया। पूर्ण होने पर मैं आपको डीएम करूँगा!",
        "verification_requested":     "🔎 सत्यापन अनुरोध भेजा गया! जाँच पूरी होने पर डीएम मिल जाएगी!",
        "setup_missing":             "⚠️ इस सर्वर ने अभी तक कोई सत्यापन भूमिका सेट नहीं की है। कृपया एक व्यवस्थापक से संपर्क करें।",
        "not_18_plus":               "❌ VRChat के अनुसार आप 18+ नहीं हैं। त्रुटि होने पर एक व्यवस्थापक से संपर्क करें।",
        "support_info":              "सत्यापन में सहायता चाहिए?\n- सहायता के लिए सर्वर व्यवस्थापक से संपर्क करें\n- या हमारी सहायता पृष्ठ देखें: https://esattotech.com/contact-us/\n\nयदि यह त्रुटिपूर्ण है, तो हमें बताएं!",
        "subscription_info":         "मैं यह नि:शुल्क प्रदान कर रहा हूं, लेकिन यदि आप मेरा समर्थन करना चाहते हैं, तो मेरा Ko-fi यहाँ देखें: {kofi_link}. आपके समर्थन के लिए धन्यवाद!",
    "settings_saved": "✅ सेटिंग्स सहेजी गईं!",
    "invalid_vrc_id_input":      "❌ ऐसा लगता है कि आपने VRChat userID के बजाय अपना display name दर्ज किया है।\nकृपया पूर्ण प्रोफ़ाइल URL या अपना userID दर्ज करें (जो हमेशा `usr_` से शुरू होता है)।\nhttps://imgur.com/a/EEl6ekH",
    "code_not_found":            "❌ हमें आपकी VRChat बायो में आपका कोड नहीं मिला। कृपया फिर से प्रयास करें।",
    "nickname_updated":          "🔔 आपका उपनाम {display_name} में अपडेट किया गया है।",
    "nickname_update_failed":    "⚠️ हम आपका उपनाम अपडेट नहीं कर सके।",
    "setup_success":             "✅ सर्वर कॉन्फ़िग {action} सफल।\nसत्यापित भूमिका सेट: `{role}` (ID={role_id})",
    "setup_unverified_set":      "\nहटाई जाने वाली 'अनवेरिफ़ाइड' भूमिका: `{role}` (ID={role_id})",
    "setup_unverified_missing":  "\n('अनवेरिफ़ाइड' भूमिका सेट नहीं है; सत्यापन पर कोई भूमिका नहीं हटेगी।)",
        "instructions_title": "VRChat सत्यापन बॉट का उपयोग कैसे करें",
        "instructions_desc": "**इन चरणों का पालन करें** अपने 18+ स्थिति की पुष्टि करने के लिए:\n\n" +
                              "1. क्लिक करें **सत्यापन प्रारंभ करें** बटन (यदि दिखाया गया हो) या कहीं भी टाइप करें `/vrcverify`।\n" +
                              "2. यदि आप नए हैं, तो आपसे आपका VRChat उपयोगकर्ता नाम पूछा जाएगा\n" +
                              "3. बॉट आपको एक अद्वितीय कोड देगा - इसे अपने VRChat जैव में डालें\n" +
                              "4. अपने जैव को अपडेट करने के बाद Discord में **सत्यापित करें** पर दबाएं\n\n" +
                              "यदि आपको अतिरिक्त सहायता की आवश्यकता है, तो एक व्यवस्थापक से संपर्क करें या टाइप करें `/vrcverify_support`।",
        "btn_begin_verification": "पुष्टि शुरू करें",
        "btn_update_nickname": "उपनाम अपडेट करें",
        "settings_intro": "⚙️ **VRChat सत्यापन सेटिंग्स**\n\n1.) **स्वचालित उपनाम परिवर्तन सक्षम करें**\n   उपयोगकर्ताओं के Discord उपनामों को उनके VRChat प्रदर्शन नामों के साथ मेल खाने के लिए स्वचालित रूप से अपडेट करें।\n   वर्तमान: **{current}**",
    },

    "ar": {
        "not_verified":               "⚠️ لم تقم بالتحقق بعد. الرجاء النقر على **بدء التحقق** أولاً.",
        "already_verified":           "✅ أنت مُحقق بالفعل! تم تعيين الدور (أو إعادة تعيينه).",
        "recheck_started":            "🔎 نقوم بإعادة التحقق من حالة 18+ في VRChat الخاصة بك. إذا قمت بتحديث التحقق من العمر، ستتلقى رسالة خاصة قريبًا!",
        "dm_role_success":            "✅ لقد تم التحقق منك ومنحت **{role}** في **{server}**!",
        "nickname_update_requested":  "🔄 تم إرسال طلب تحديث الاسم المستعار. سأرسل لك رسالة خاصة عند الانتهاء!",
        "verification_requested":     "🔎 تم إرسال طلب التحقق! سأرسل لك رسالة خاصة بمجرد الانتهاء من التحقق من ملفك الشخصي في VRChat!",
        "setup_missing":             "⚠️ لم يتم إعداد دور التحقق في هذا الخادم بعد. الرجاء الاتصال بمسؤول.",
        "not_18_plus":               "❌ وفقًا لـ VRChat، أنت لست 18+. اتصل بمسؤول إذا كان هذا خطأً.",
        "support_info":              "تحتاج مساعدة في التحقق؟\n- اتصل بمسؤول الخادم للمساعدة\n- أو قم بزيارة صفحة الدعم: https://esattotech.com/contact-us/\n\nإذا كان هذا خطأً، فأخبرنا!",
        "subscription_info":         "أقدم هذا مجانًا، ولكن إذا كنت ترغب في دعمي، يمكنك العثور على Ko-fi الخاص بي هنا: {kofi_link}. شكرًا لدعمك المستمر!",
    "settings_saved": "✅ تم حفظ الإعدادات!",
    "invalid_vrc_id_input":      "❌ يبدو أنك أدخلت اسم العرض بدلاً من معرف مستخدم VRChat. الرجاء إدخال رابط الملف الشخصي الكامل أو معرف المستخدم الخاص بك (الذي يبدأ دائمًا بـ `usr_`).\nhttps://imgur.com/a/EEl6ekH",
    "code_not_found":            "❌ لم نعثر على الرمز في سيرتك في VRChat. يرجى المحاولة مرة أخرى.",
    "nickname_updated":          "🔔 تم تحديث لقبك إلى {display_name}.",
    "nickname_update_failed":    "⚠️ لم نتمكن من تحديث لقبك.",
    "setup_success":             "✅ تم {action} إعداد الخادم بنجاح.\nتم تعيين الدور الموثّق إلى: `{role}` (المعرف={role_id})",
    "setup_unverified_set":      "\nالدور غير الموثّق المطلوب إزالته: `{role}` (المعرف={role_id})",
    "setup_unverified_missing":  "\n(لم يتم تعيين دور غير موثّق؛ لن تتم إزالة أي دور عند التحقق.)",
        "instructions_title": "كيفية استخدام روبوت التحقق من VRChat",
        "instructions_desc": "**اتبع هذه الخطوات** للتحقق من حالة 18+ الخاصة بك:\n\n" +
                              "1. انقر على زر **بدء التحقق** (إذا تم عرضه) أو اكتب `/vrcverify` في أي مكان.\n" +
                              "2. إذا كنت جديدًا، سيُطلب منك اسم مستخدم VRChat الخاص بك\n" +
                              "3. سيعطيك الروبوت رمزًا فريدًا - ضع هذا في سيرتك الذاتية على VRChat\n" +
                              "4. اضغط على **تحقق** في Discord بمجرد تحديث سيرتك الذاتية\n\n" +
                              "إذا كنت بحاجة إلى مساعدة إضافية، فاتصل بالمسؤول أو اكتب `/vrcverify_support`.",
        "btn_begin_verification": "بدء التحقق",
        "btn_update_nickname": "تحديث الاسم المستعار",
        "settings_intro": "⚙️ **إعدادات تحقق VRChat**\n\n1.) **تمكين تغيير اللقب التلقائي**\n   تحديث ألقاب Discord الخاصة بالمستخدمين تلقائيًا لتتوافق مع أسماء عرض VRChat الخاصة بهم.\n   الحالي: **{current}**",
    },

    "bn": {
        "not_verified":               "⚠️ আপনি এখনও যাচাইপ্রক্রিয়া করেননি। অনুগ্রহ করে প্রথমে **যাচাই শুরু করুন** ক্লিক করুন।",
        "already_verified":           "✅ আপনি ইতিমধ্যেই যাচাই করা হয়েছে! ভূমিকা প্রদান করা হয়েছে (অথবা পুনরায় প্রদান করা হয়েছে)।",
        "recheck_started":            "🔎 আমরা আপনার VRChat 18+ অবস্থা পুনরায় যাচাই করছি। যদি আপনি আপনার বয়স যাচাই আপডেট করে থাকেন, আপনি শীঘ্রই একটি ডিএম পাবেন!",
        "dm_role_success":            "✅ আপনি যাচাইপ্রক্রিয়া সম্পন্ন করেছেন এবং **{role}** ভূমিকা **{server}**-এ পেয়েছেন!",
        "nickname_update_requested":  "🔄 ডাকনাম আপডেটের অনুরোধ পাঠানো হয়েছে। সম্পন্ন হলে আমি আপনাকে ডিএম করবো!",
        "verification_requested":     "🔎 যাচাইকরণ অনুরোধ প্রেরণ করা হয়েছে! VRChat প্রোফাইল পরীক্ষা শেষ হলে আমি আপনাকে ডিএম করবো!",
        "setup_missing":             "⚠️ এই সার্ভারে এখনও যাচাইকরণ ভূমিকা সেট করা হয়নি। অনুগ্রহ করে একজন অ্যাডমিনের সাথে যোগাযোগ করুন।",
        "not_18_plus":               "❌ VRChat অনুযায়ী আপনি 18+ নন। যদি এটি ত্রুটি হয়, একজন অ্যাডমিনের সাথে যোগাযোগ করুন।",
        "support_info":              "যাচাই নিয়ে সহায়তা প্রয়োজন?\n- সহায়তার জন্য সার্ভার অ্যাডমিনের সাথে যোগাযোগ করুন\n- অথবা আমাদের সমর্থন পৃষ্ঠা দেখুন: https://esattotech.com/contact-us/\n\nযদি এটি ত্রুটি হয়, আমাদের জানান!",
        "subscription_info":         "এটি আমি বিনামূল্যে প্রদান করছি, তবে আপনি যদি আমাকে সমর্থন করতে চান তবে আমার Ko-fi এখানে দেখুন: {kofi_link}. আপনাদের সমর্থনের জন্য ধন্যবাদ!",
    "settings_saved": "✅ সেটিংস সংরক্ষিত হয়েছে!",
    "invalid_vrc_id_input":      "❌ মনে হচ্ছে আপনি VRChat userID-এর বদলে আপনার প্রদর্শন নাম লিখেছেন।\nঅনুগ্রহ করে সম্পূর্ণ প্রোফাইল URL বা আপনার userID লিখুন (যা সবসময় `usr_` দিয়ে শুরু হয়)।\nhttps://imgur.com/a/EEl6ekH",
    "code_not_found":            "❌ আপনার VRChat বায়োতে আমরা কোডটি খুঁজে পাইনি। অনুগ্রহ করে আবার চেষ্টা করুন।",
    "nickname_updated":          "🔔 আপনার ডাকনাম {display_name} এ আপডেট হয়েছে।",
    "nickname_update_failed":    "⚠️ আমরা আপনার ডাকনাম আপডেট করতে পারিনি।",
    "setup_success":             "✅ সার্ভার কনফিগ {action} সফল।\nযাচাইকৃত ভূমিকা সেট করা হয়েছে: `{role}` (ID={role_id})",
    "setup_unverified_set":      "\nমুছে ফেলার জন্য 'অনভেরিফাইড' ভূমিকা: `{role}` (ID={role_id})",
    "setup_unverified_missing":  "\n('অনভেরিফাইড' ভূমিকা সেট করা নেই; যাচাইয়ের সময় কোনো ভূমিকা সরানো হবে না।)",
        "instructions_title": "VRChat যাচাইকরণ বট ব্যবহার করার জন্য নির্দেশিকা",
        "instructions_desc": "**এই পদক্ষেপগুলি অনুসরণ করুন** আপনার 18+ স্থিতি যাচাই করতে:\n\n" +
                              "1. ক্লিক করুন **যাচাই শুরু করুন** বোতামে (যদি প্রদর্শিত হয়) অথবা যেকোনো জায়গায় টাইপ করুন `/vrcverify`।\n" +
                              "2. যদি আপনি নতুন হন, তবে আপনাকে আপনার VRChat ব্যবহারকারীর নাম দেওয়ার জন্য বলা হবে\n" +
                              "3. বট আপনাকে একটি অনন্য কোড দেবে - এটি আপনার VRChat জীবনীতে রাখুন\n" +
                              "4. আপনার জীবনী আপডেট হলে Discord-এ **যাচাই করুন** এ ক্লিক করুন\n\n" +
                              "যদি আপনার অতিরিক্ত সহায়তার প্রয়োজন হয়, তবে একটি প্রশাসকের সাথে যোগাযোগ করুন বা টাইপ করুন `/vrcverify_support`।",
        "btn_begin_verification": "যাচাই শুরু করুন",
        "btn_update_nickname": "ডাকনাম আপডেট করুন",
        "settings_intro": "⚙️ **VRChat যাচাইকরণ सेटिंग्स**\n\n1.) **স্বয়ংক্রিয় ডাকনাম ਬਦਲਣਾ ਯਕੀਨੀ ਬਣਾਓ**\n   ਵਰਤੋਂਕਾਰਾਂ ਦੇ Discord ਨਿਕਨੇਮਾਂ ਨੂੰ ਉਹਨਾਂ ਦੇ VRChat ਪ੍ਰਦਰਸ਼ਨ ਨਾਮਾਂ ਦੇ ਨਾਲ ਮੇਲ ਖਾਣ ਲਈ ਆਟੋਮੈਟਿਕ ਤੌਰ 'ਤੇ ਅਪਡੇਟ ਕਰੋ।\n   ਮੌਜੂਦਾ: **{current}**",
    },

    "pt-BR": {
        "not_verified":               "⚠️ Você ainda não está verificado. Por favor, clique em **Iniciar Verificação** primeiro.",
        "already_verified":           "✅ Você já está verificado! Papel atribuído (ou reatribuído).",
        "recheck_started":            "🔎 Estamos verificando novamente seu status 18+ no VRChat. Se você atualizou sua verificação de idade, receberá uma DM em breve!",
        "dm_role_success":            "✅ Você foi verificado e recebeu **{role}** em **{server}**!",
        "nickname_update_requested":  "🔄 Solicitação de atualização de apelido enviada. Te enviarei uma DM quando estiver pronto!",
        "verification_requested":     "🔎 Solicitação de verificação enviada! Enviarei uma DM quando terminarmos de verificar seu perfil no VRChat!",
        "setup_missing":             "⚠️ Este servidor ainda não configurou uma função de verificação. Por favor, contate um administrador.",
        "not_18_plus":               "❌ De acordo com o VRChat, você não tem 18+. Contate um administrador se isso for um erro.",
        "support_info":              "Precisa de ajuda com a verificação?\n- Contate um administrador do servidor\n- Ou visite nossa página de suporte: https://esattotech.com/contact-us/\n\nSe isso for um erro, por favor nos avise!",
        "subscription_info":         "Estou oferecendo isso gratuitamente, mas se quiser me apoiar, você pode encontrar meu Ko-fi aqui: {kofi_link}. Obrigado pelo apoio!",
    "settings_saved": "✅ Configurações salvas!",
    "invalid_vrc_id_input":      "❌ Parece que você digitou seu nome de exibição em vez do seu userID do VRChat.\nInsira a URL completa do perfil ou seu userID (que sempre começa com `usr_`).\nhttps://imgur.com/a/EEl6ekH",
    "code_not_found":            "❌ Não encontramos seu código na sua bio do VRChat. Tente novamente.",
    "nickname_updated":          "🔔 Seu apelido foi atualizado para {display_name}.",
    "nickname_update_failed":    "⚠️ Não foi possível atualizar seu apelido.",
    "setup_success":             "✅ Configuração do servidor {action}.\nFunção verificada definida como: `{role}` (ID={role_id})",
    "setup_unverified_set":      "\nFunção não verificada a remover: `{role}` (ID={role_id})",
    "setup_unverified_missing":  "\n(Função não verificada não definida; nenhuma função será removida na verificação.)",
        "instructions_title": "Como usar o bot de verificação do VRChat",
        "instructions_desc": "**Siga estas etapas** para verificar seu status 18+:\n\n" +
                              "1. Clique no botão **Iniciar Verificação** (se mostrado) ou digite `/vrcverify` em qualquer lugar.\n" +
                              "2. Se você é novo, será solicitado seu nome de usuário do VRChat\n" +
                              "3. O bot lhe dará um código único - coloque isso na sua biografia do VRChat\n" +
                              "4. Pressione **Verificar** no Discord assim que sua biografia estiver atualizada\n\n" +
                              "Se você precisar de ajuda adicional, entre em contato com um administrador ou digite `/vrcverify_support`.",
        "btn_begin_verification": "Iniciar Verificação",
        "btn_update_nickname": "Atualizar Apelido",
        "settings_intro": "⚙️ **Configurações de Verificação do VRChat**\n\n1.) **Ativar alteração automática de apelido**\n   Atualize automaticamente os apelidos do Discord dos usuários para corresponder aos seus nomes de exibição do VRChat.\n   Atual: **{current}**",
    },

    "ru": {
        "not_verified":               "⚠️ Вы еще не прошли проверку. Пожалуйста, сначала нажмите **Начать проверку**.",
        "already_verified":           "✅ Вы уже проверены! Роль назначена (или переназначена).",
        "recheck_started":            "🔎 Мы повторно проверяем ваш статус 18+ в VRChat. Если вы обновили проверку возраста, вскоре получите личное сообщение!",
        "dm_role_success":            "✅ Вы прошли проверку и получили роль **{role}** на **{server}**!",
        "nickname_update_requested":  "🔄 Запрошено обновление ника. Я отправлю вам личное сообщение, когда все будет готово!",
        "verification_requested":     "🔎 Запрос на проверку отправлен! Я пришлю вам личное сообщение, как только завершу проверку вашего профиля VRChat!",
        "setup_missing":             "⚠️ Этот сервер еще не настроил роль проверки. Пожалуйста, свяжитесь с администратором.",
        "not_18_plus":               "❌ По данным VRChat вам нет 18+. Свяжитесь с администратором, если это ошибка.",
        "support_info":              "Нужна помощь с проверкой?\n- Обратитесь к администратору сервера\n- Или посетите нашу страницу поддержки: https://esattotech.com/contact-us/\n\nЕсли это ошибка, дайте нам знать!",
        "subscription_info":         "Я предоставляю это бесплатно, но если вы хотите меня поддержать, вы можете найти мой Ko-fi здесь: {kofi_link}. Спасибо за вашу поддержку!",
    "settings_saved": "✅ Настройки сохранены!",
    "invalid_vrc_id_input":      "❌ Похоже, вы ввели отображаемое имя вместо идентификатора пользователя VRChat.\nВведите полный URL профиля или ваш userID (всегда начинается с `usr_`).\nhttps://imgur.com/a/EEl6ekH",
    "code_not_found":            "❌ Мы не нашли ваш код в вашей биографии VRChat. Пожалуйста, попробуйте ещё раз.",
    "nickname_updated":          "🔔 Ваш ник обновлён на {display_name}.",
    "nickname_update_failed":    "⚠️ Не удалось обновить ваш ник.",
    "setup_success":             "✅ Успешно {action} конфигурацию сервера.\nУстановлена проверенная роль: `{role}` (ID={role_id})",
    "setup_unverified_set":      "\nНепроверенная роль для удаления: `{role}` (ID={role_id})",
    "setup_unverified_missing":  "\n(Непроверенная роль не задана; при проверке роли удаляться не будут.)",
        "instructions_title": "Как использовать бот проверки VRChat",
        "instructions_desc": "**Следуйте этим шагам**, чтобы проверить свой статус 18+:\n\n" +
                              "1. Нажмите кнопку **Начать проверку** (если отображается) или введите `/vrcverify` в любом месте.\n" +
                              "2. Если вы новичок, вас попросят ввести имя пользователя VRChat\n" +
                              "3. Бот даст вам уникальный код - поместите его в свою биографию VRChat\n" +
                              "4. Нажмите **Проверить** в Discord, как только ваша биография будет обновлена\n\n" +
                              "Если вам нужна дополнительная помощь, свяжитесь с администратором или введите `/vrcverify_support`.",
        "btn_begin_verification": "Начать проверку",
        "btn_update_nickname": "Обновить никнейм",
        "settings_intro": "⚙️ **Настройки проверки VRChat**\n\n1.) **Включить автоматическую смену никнейма**\n   Автоматически обновлять никнеймы пользователей Discord в соответствии с их отображаемыми именами VRChat.\n   Текущий: **{current}**",
    },

    "pa-IN": {
        "not_verified":               "⚠️ ਤੁਸੀਂ ਅਜੇ ਤੱਕ ਸਤਿਆਪਿਤ ਨਹੀਂ ਹੋ। ਕਿਰਪਾ ਕਰਕੇ ਪਹਿਲਾਂ **ਪ੍ਰਮਾਣੀਕਰਨ ਸ਼ੁਰੂ ਕਰੋ** 'ਤੇ ਕਲਿੱਕ ਕਰੋ।",
        "already_verified":           "✅ ਤੁਸੀਂ ਪਹਿਲਾਂ ਹੀ ਸਤਿਆਪਿਤ ਹੋ! ਭੂਮਿਕਾ ਨਿਰਧਾਰਿਤ (ਜਾਂ ਦੁਬਾਰਾ ਨਿਰਧਾਰਿਤ) ਕੀਤੀ ਗਈ।",
        "recheck_started":            "🔎 ਅਸੀਂ ਤੁਹਾਡੀ VRChat 18+ ਸਥਿਤੀ ਨੂੰ ਦੁਬਾਰਾ ਜਾਂਚ ਰਹੇ ਹਾਂ। ਜੇ ਤੁਸੀਂ ਆਪਣੀ ਉਮਰ ਸਤਿਆਪਨ ਅੱਪਡੇਟ ਕੀਤੀ ਹੈ, ਤਾਂ ਤੁਹਾਨੂੰ ਜਲਦੀ ਹੀ ਇੱਕ ਡੀਐਮ ਮਿਲੇਗੀ!",
        "dm_role_success":            "✅ ਤੁਹਾਨੂੰ ਸਤਿਆਪਿਤ ਕੀਤਾ ਗਿਆ ਹੈ ਅਤੇ **{role}** ਭੂਮਿਕਾ **{server}** ਵਿੱਚ ਦਿੱਤੀ ਗਈ ਹੈ!",
        "nickname_update_requested":  "🔄 ਨਿਕਨੇਮ ਅਪਡੇਟ ਕਰਨ ਦੀ ਬੇਨਤੀ ਭੇਤੀ ਗਈ। ਜਦੋਂ ਇਹ ਹੋ ਜਾਵੇਗਾ ਤਾਂ ਮੈਂ ਤੁਹਾਨੂੰ DM ਕਰਾਂਗਾ!",
        "verification_requested":     "🔎 ਪ੍ਰਮਾਣੀਕਰਨ ਬੇਨਤੀ ਭੇਜ ਦਿੱਤੀ ਗਈ! VRChat ਪ੍ਰੋਫਾਈਲ ਚੈਕ ਹੋਣ 'ਤੇ ਮੈਂ ਤੁਹਾਨੂੰ DM ਭੇਜਾਂਗਾ!",
        "setup_missing":             "⚠️ ਇਸ ਸਰਵਰ 'ਤੇ ਪ੍ਰਮਾਣੀਕਰਨ ਭੂਮਿਕਾ ਅਜੇ ਤੱਕ ਸੈੱਟ ਨਹੀਂ ਕੀਤੀ ਗਈ। ਕਿਰਪਾ ਕਰਕੇ ਇੱਕ ਐਡਮਿਨ ਨਾਲ ਸੰਪਰਕ ਕਰੋ।",
        "not_18_plus":               "❌ VRChat ਅਨੁਸਾਰ ਤੁਹਾਡੇ ਕੋਲ 18+ ਨਹੀਂ ਹੈ। ਜੇ ਇਹ ਗਲਤੀ ਹੈ, ਤਾਂ ਇੱਕ ਐਡਮਿਨ ਨਾਲ ਸੰਪਰਕ ਕਰੋ।",
        "support_info":              "ਪ੍ਰਮਾਣੀਕਰਨ ਵਿੱਚ ਸਹਾਇਤਾ ਚਾਹੀਦੀ ਹੈ?\n- ਸਹਾਇਤਾ ਲਈ ਸਰਵਰ ਐਡਮਿਨ ਨਾਲ ਸੰਪਰਕ ਕਰੋ\n- ਜਾਂ ਆਪਣੀ ਸਹਾਇਤਾ ਸਫ਼ਾ ਵੇਖੋ: https://esattotech.com/contact-us/\n\nਜੇ ਇਹ ਗਲਤੀ ਹੈ, ਤਾਂ ਸਾਨੂੰ ਦੱਸੋ!",
        "subscription_info":         "ਮੈਂ ਇਹ ਮੁਫ਼ਤ ਪ੍ਰਦਾਨ ਕਰ ਰਿਹਾ ਹਾਂ, ਪਰ ਜੇ ਤੁਸੀਂ ਮੇਰੀ ਸਹਾਇਤਾ ਕਰਨਾ ਚਾਹੁੰਦੇ ਹੋ, ਤਾਂ ਮੇਰਾ Ko-fi ਇਥੇ ਵੇਖੋ: {kofi_link}. ਤੁਹਾਡੇ ਸਹਿਯੋਗ ਲਈ ਧੰਨਵਾਦ!",
    "settings_saved": "✅ ਸੈਟਿੰਗਸ ਸੁਰੱਖਿਅਤ ਕੀਤੀਆਂ ਗਈਆਂ!",
    "invalid_vrc_id_input":      "❌ ਲੱਗਦਾ ਹੈ ਤੁਸੀਂ VRChat userID ਦੀ ਬਜਾਏ ਆਪਣਾ ਡਿਸਪਲੇ ਨਾਂ ਦਰਜ ਕੀਤਾ ਹੈ।\nਕਿਰਪਾ ਕਰਕੇ ਪੂਰਾ ਪ੍ਰੋਫ਼ਾਈਲ URL ਜਾਂ ਆਪਣਾ userID ਦਰਜ ਕਰੋ (ਜੋ ਹਮੇਸ਼ਾਂ `usr_` ਨਾਲ ਸ਼ੁਰੂ ਹੁੰਦਾ ਹੈ)।\nhttps://imgur.com/a/EEl6ekH",
    "code_not_found":            "❌ ਅਸੀਂ ਤੁਹਾਡੇ VRChat ਬਾਇਓ ਵਿੱਚ ਤੁਹਾਡਾ ਕੋਡ ਨਹੀਂ ਲੱਭ ਸਕੇ। ਕਿਰਪਾ ਕਰਕੇ ਮੁੜ ਕੋਸ਼ਿਸ਼ ਕਰੋ।",
    "nickname_updated":          "🔔 ਤੁਹਾਡਾ ਨਿਕਨੇਮ {display_name} ਵਿੱਚ ਅਪਡੇਟ ਕਰ ਦਿੱਤਾ ਗਿਆ ਹੈ।",
    "nickname_update_failed":    "⚠️ ਅਸੀਂ ਤੁਹਾਡਾ ਨਿਕਨੇਮ ਅਪਡੇਟ ਨਹੀਂ ਕਰ ਸਕੇ।",
    "setup_success":             "✅ ਸਰਵਰ ਕਨਫ਼ਿਗ {action} ਸਫਲ।\nਪ੍ਰਮਾਣਿਤ ਰੋਲ ਸੈੱਟ ਕੀਤਾ: `{role}` (ID={role_id})",
    "setup_unverified_set":      "\nਹਟਾਉਣ ਲਈ 'ਅਨਵੈਰੀਫਾਇਡ' ਰੋਲ: `{role}` (ID={role_id})",
    "setup_unverified_missing":  "\n('ਅਨਵੈਰੀਫਾਇਡ' ਰੋਲ ਸੈੱਟ ਨਹੀਂ; ਤਸਦੀਕ ਹੋਣ 'ਤੇ ਕੋਈ ਰੋਲ ਨਹੀਂ ਹਟਾਇਆ ਜਾਵੇਗਾ।)",
        "instructions_title": "VRChat ਪ੍ਰਮਾਣੀਕਰਨ ਬੋਟ ਦੀ ਵਰਤੋਂ ਕਰਨ ਲਈ ਹਦਾਇਤਾਂ",
        "instructions_desc": "**ਇਹਨਾਂ ਕਦਮਾਂ ਦੀ ਪਾਲਣਾ ਕਰੋ** ਆਪਣੇ 18+ ਸਥਿਤੀ ਦੀ ਪੁਸ਼ਟੀ ਕਰਨ ਲਈ:\n\n" +
                              "1. 'ਤੇ ਕਲਿੱਕ ਕਰੋ **ਪ੍ਰਮਾਣੀਕਰਨ ਸ਼ੁਰੂ ਕਰੋ** ਬਟਨ (ਜੇ ਦਿਖਾਇਆ ਗਿਆ ਹੋਵੇ) ਜਾਂ ਕਿਸੇ ਵੀ ਜਗ੍ਹਾ ਟਾਈਪ ਕਰੋ `/vrcverify`।\n" +
                              "2. ਜੇ ਤੁਸੀਂ ਨਵੇਂ ਹੋ, ਤਾਂ ਤੁਹਾਡੇ ਤੋਂ ਤੁਹਾਡਾ VRChat ਯੂਜ਼ਰ ਨਾਮ ਪੁੱਛਿਆ ਜਾਵੇਗਾ\n" +
                              "3. ਬੋਟ ਤੁਹਾਨੂੰ ਇੱਕ ਵਿਲੱਖਣ ਕੋਡ ਦੇਵੇਗਾ - ਇਸਨੂੰ ਆਪਣੇ VRChat ਜੀਵਨ ਚਰਿਤ੍ਰ ਵਿੱਚ ਰੱਖੋ\n" +
                              "4. ਆਪਣੇ ਜੀਵਨ ਚਰਿਤ੍ਰ ਨੂੰ ਅਪਡੇਟ ਕਰਨ ਤੋਂ ਬਾਅਦ Discord ਵਿੱਚ **ਸत्यਾਪਿਤ ਕਰੋ** 'ਤੇ ਦਬਾਓ\n\n" +
                              "ਜੇ ਤੁਹਾਨੂੰ ਵਾਧੂ ਸਹਾਇਤਾ ਦੀ ਲੋੜ ਹੈ, ਤਾਂ ਇੱਕ ਪ੍ਰਬੰਧਕ ਨਾਲ ਸੰਪਰਕ ਕਰੋ ਜਾਂ ਟਾਈਪ ਕਰੋ `/vrcverify_support`।",
        "btn_begin_verification": "ਪ੍ਰਮਾਣੀਕਰਨ ਸ਼ੁਰੂ ਕਰੋ",
        "btn_update_nickname": "ਨਿਕਨੇਮ ਅਪਡੇਟ ਕਰੋ",
        "settings_intro": "⚙️ **VRChat ਪ੍ਰਮਾਣੀਕਰਨ ਸੈਟਿੰਗਸ**\n\n1.) **ਆਟੋ-ਨਿਕਨੇਮ ਬਦਲਣਾ ਯਕੀਨੀ ਬਣਾਓ**\n   ਵਰਤੋਂਕਾਰਾਂ ਦੇ Discord ਨਿਕਨੇਮਾਂ ਨੂੰ ਉਹਨਾਂ ਦੇ VRChat ਪ੍ਰਦਰਸ਼ਨ ਨਾਮਾਂ ਦੇ ਨਾਲ ਮੇਲ ਖਾਣ ਲਈ ਆਟੋਮੈਟਿਕ ਤੌਰ 'ਤੇ ਅਪਡੇਟ ਕਰੋ।\n   ਮੌਜੂਦਾ: **{current}**",
    }
}
