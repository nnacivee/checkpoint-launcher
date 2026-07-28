import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "ui" / "center-control-layouts.html").read_text(
    encoding="utf-8"
)
I18N = (ROOT / "ui" / "assets" / "i18n.js").read_text(encoding="utf-8")
CSS = (ROOT / "ui" / "assets" / "release-polish.css").read_text(
    encoding="utf-8"
)


class Launcher16619UiTests(unittest.TestCase):
    def test_client_state_refreshes_while_idle(self):
        self.assertIn(
            "setInterval(()=>refreshClientStateIfIdle(),50000)", HTML
        )
        self.assertIn(
            "!maintenanceBusy&&!clientUpdating"
            "&&!clientBusyModes.has(clientStateMode)",
            HTML,
        )
        self.assertIn(
            "window.addEventListener('online',()=>{"
            "document.body.dataset.network='online';"
            "refreshClientStateIfIdle(true)",
            HTML,
        )
        self.assertIn(
            "window.addEventListener('focus',()=>{"
            "refreshClientStateIfIdle()",
            HTML,
        )
        self.assertIn(
            "visibilitychange',()=>{if(!document.hidden){"
            "refreshClientStateIfIdle()",
            HTML,
        )

    def test_launcher_update_check_is_minutely_and_status_aware(self):
        self.assertIn("scheduleLauncherUpdateCheck(60000)", HTML)
        self.assertNotIn("scheduleLauncherUpdateCheck(300000)", HTML)
        self.assertIn("result?.status==='current'", HTML)
        self.assertIn("result?.status==='unavailable'", HTML)
        self.assertIn(
            "if(updateApplying){scheduleLauncherUpdateCheck(60000)", HTML
        )

    def test_update_and_progress_copy_is_not_duplicated_in_tooltip(self):
        self.assertIn(
            "mode==='warning'&&clientNeedsUpdate?"
            "'Обновить и играть':'Играть'",
            HTML,
        )
        self.assertIn(
            "t('{progress}% · всего'"
            ",{progress:Math.round(clientUpdateProgress)})",
            HTML,
        )
        self.assertIn(
            "t('Этап {step} · {action}'"
            ",{step:stage.step,action:currentAction})",
            HTML,
        )
        self.assertIn("state.removeAttribute('title')", HTML)
        self.assertIn(
            "if(mode==='error')state.title=stateText;"
            "else state.removeAttribute('title')",
            HTML,
        )
        self.assertIn(
            "if(mode==='error')button.title=stateText;"
            "else button.removeAttribute('title')",
            HTML,
        )
        self.assertNotIn("state.title=progressTooltip", HTML)

    def test_resume_and_human_error_states_have_three_languages(self):
        expected_rows = (
            '["Продолжаем загрузку","Продовжуємо завантаження",'
            '"Resuming download"]',
            '["Обновить и играть","Оновити й грати","Update and play"]',
            '["Нет соединения — проверьте интернет и повторите",'
            '"Немає з’єднання — перевірте інтернет і повторіть",'
            '"No connection — check your internet and retry"]',
            '["Недостаточно места — освободите место на диске",'
            '"Недостатньо місця — звільніть місце на диску",'
            '"Not enough space — free up disk space"]',
            '["Нет сети — для первой установки нужен интернет",'
            '"Немає мережі — для першого встановлення потрібен інтернет",'
            '"No network — internet is required for the first installation"]',
            '["Требуется обновление настроек",'
            '"Потрібне оновлення налаштувань",'
            '"Settings update required"]',
        )
        for row in expected_rows:
            with self.subTest(row=row):
                self.assertIn(row, I18N)
        self.assertIn("function friendlyClientError(message)", HTML)
        self.assertIn("button.dataset.retry=String(mode==='error')", HTML)
        self.assertIn('.play[data-retry="true"]', CSS)

    def test_settings_success_is_only_shown_after_backend_success(self):
        self.assertIn("function apiResultFailed(result)", HTML)
        self.assertGreaterEqual(
            HTML.count("if(results.some(apiResultFailed))"), 2
        )
        self.assertIn("if(!(await saveMemory(false)))return", HTML)
        self.assertIn(
            "Не удалось применить настройки. "
            "Закройте Minecraft и повторите",
            HTML,
        )
        self.assertIn(
            '["Не удалось применить настройки. '
            'Закройте Minecraft и повторите",'
            '"Не вдалося застосувати налаштування. '
            'Закрийте Minecraft і повторіть",'
            '"Could not apply settings. Close Minecraft and try again"]',
            I18N,
        )

    def test_static_version_and_player_friendly_progress_copy_are_current(self):
        self.assertIn("data-boot-launcher>1.66.30</b>", HTML)
        compact = HTML.split(
            "function compactLaunchPhase", 1
        )[1].split("function parseLaunchStage", 1)[0]
        self.assertLess(
            compact.index("/провер|check/i"),
            compact.index("/мод|дополн|addon/i"),
        )
        for source in (
            "Файлы игры",
            "Подготовка запуска",
            "Подготовка игры",
            "Файлы сборки",
            "Загрузка файлов",
        ):
            with self.subTest(source=source):
                self.assertIn(f"'{source}'", compact)

    def test_ukrainian_repair_copy_is_grammatical(self):
        self.assertIn(
            '["Клиент восстановлен и готов к запуску.",'
            '"Клієнт відновлено та підготовлено до запуску.",'
            '"Client repaired and ready to launch."]',
            I18N,
        )

    def test_late_client_state_and_missing_repair_progress_are_safe(self):
        self.assertIn(
            "invokeApi('refresh_client_state',requestId)", HTML
        )
        self.assertIn(
            "responseId!==clientStateRequestId)return", HTML
        )
        self.assertIn(
            "if(data?.background_check)return", HTML
        )
        self.assertIn(
            "Number.isFinite(parsedProgress)?"
            "parsedProgress:clientUpdateProgress",
            HTML,
        )
        self.assertIn(
            '["{progress}% · всего","{progress}% · загалом",'
            '"{progress}% · total"]',
            I18N,
        )


if __name__ == "__main__":
    unittest.main()
