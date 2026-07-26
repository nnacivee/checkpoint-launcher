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
        )
        for row in expected_rows:
            with self.subTest(row=row):
                self.assertIn(row, I18N)
        self.assertIn("function friendlyClientError(message)", HTML)
        self.assertIn("button.dataset.retry=String(mode==='error')", HTML)
        self.assertIn('.play[data-retry="true"]', CSS)

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
