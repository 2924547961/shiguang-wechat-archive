"""Render a fictional relationship report in the explicit --demo UI."""
import json
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright


def main():
    url, output = sys.argv[1], Path(sys.argv[2]); output.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(channel='msedge', headless=True)
        page = browser.new_page(viewport={'width': 1440, 'height': 940}, device_scale_factor=1,
                                bypass_csp=True, reduced_motion='reduce')
        errors = []; page.on('pageerror', lambda exc: errors.append(str(exc)))
        page.goto(url)
        page.locator('#demo-label').wait_for()
        page.locator('#navigation [data-nav="insights"]').click()
        with page.expect_response(lambda response: '/api/insights/saved?' in response.url):
            page.locator('input[name="insight-contact"][value="demo_xiaoyu"]').check()
        page.locator('#insight-skills textarea').first.wait_for()
        report = {
            'id': 'fixture-mirror', 'conversation': {'id': 'demo_xiaoyu', 'title': '小予', 'kind': 'direct'},
            'mode': 'both', 'total': 120, 'sampled': 36, 'from_time': '2025-02-01 09:00',
            'to_time': '2025-03-12 21:00', 'warnings': [],
            'evidence': [
                {'id': 'E1', 'time': '2025-02-01 09:00', 'side': '我', 'text': '这周末能不能提前定下时间？'},
                {'id': 'E2', 'time': '2025-02-01 09:03', 'side': '对方', 'text': '我明天再看安排。'},
                {'id': 'E3', 'time': '2025-02-01 09:05', 'side': '我', 'text': '好的，明晚再确认。'},
            ],
            'sections': {
                'self': {'strengths': ['愿意提出具体时间'], 'tendencies': [
                    {'dimension': '尽责性', 'observation': '在安排事情时倾向于提前确认。',
                     'confidence': '中', 'refs': ['E1'], 'alternate': '也可能只因为这次需要预订。'}],
                    'emotion': '这段样本里未直接表达强烈情绪。', 'communication': '偏好明确安排。',
                    'summary_refs': ['E1', 'E3']},
                'other': {'strengths': ['给出了之后确认的时间'], 'tendencies': [],
                          'emotion': '样本不足。', 'communication': '暂未定下安排。', 'summary_refs': ['E2']},
                'events': [{'trigger': '安排周末时间', 'a_behavior': '希望提前确认',
                            'b_behavior': '表示明天再看', 'possible_needs': '双方可能需要不同的确定时间。',
                            'refs': ['E1', 'E2']}],
                'cycle': {'name': '提议—等待—再确认', 'steps': ['提出时间', '暂缓决定', '约定复核'],
                          'refs': ['E1', 'E2', 'E3'], 'alternate': '这也可能只是一次普通的排程。'},
                'changes': [{'behavior': '把希望确认的截止时间说清楚', 'refs': ['E1', 'E3'],
                             'trigger': '需要安排周末活动', 'old_behavior': '重复催促',
                             'new_behavior': '说明原因并约定确认时间',
                             'example': '我想今天订票；我们明晚八点确认可以吗？',
                             'card': {'short_gain': '立即得到回应', 'long_cost': '可能增加压力',
                                      'supporting_evidence': '本次确实需要提前安排。',
                                      'counter_evidence': '对方已提出明天再看。',
                                      'alternate_thought': '可以先说出具体需要，再约定时间。'}}],
                'plan': [{'day': i, 'phase': ['观察', '暂停', '尝试', '复盘'][min(3, (i-1)//8)],
                          'task': '记录一次实际沟通，并写下可以调整的一句话。'} for i in range(1, 31)]
            }
        }
        page.evaluate('(report) => { insightReport = report; return Mirror.render(); }', report)
        page.locator('.mirror-module').first.wait_for()
        assert page.locator('.mirror-module').count() == 6
        page.locator('.mirror-change details summary').click()
        page.locator('[data-mirror-reflection="0"]').fill('我担心来不及订票。')
        page.locator('[data-action="mirror-card-to-practice"]').click()
        assert '我担心' in page.locator('#mirror-practice-note').input_value()
        page.locator('.mirror-ref').first.click()
        assert page.locator('.mirror-evidence').get_attribute('open') is not None
        page.screenshot(path=str(output/'mirror-evidence.png'))
        page.locator('#content').evaluate('(el) => el.scrollTop = 0')
        page.screenshot(path=str(output/'mirror-report-top.png'))
        page.locator('.mirror-module').nth(3).scroll_into_view_if_needed()
        page.screenshot(path=str(output/'mirror-report-actions.png'))
        page.set_viewport_size({'width': 960, 'height': 760})
        page.locator('#content').evaluate('(el) => el.scrollTop = 0')
        page.screenshot(path=str(output/'mirror-report-compact.png'))
        assert page.evaluate('document.documentElement.scrollWidth <= 960')
        assert not errors, errors
        print(json.dumps({'result': 'passed', 'modules': 6, 'javascript_errors': errors}))
        browser.close()


if __name__ == '__main__': main()
