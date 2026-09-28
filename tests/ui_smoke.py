"""Run against an explicit --demo server; never exercise personal chat data."""
import json
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright


def main():
    url, output = sys.argv[1], Path(sys.argv[2]); output.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser=p.chromium.launch(channel='msedge',headless=True)
        page=browser.new_page(viewport={'width':1440,'height':940},device_scale_factor=1,bypass_csp=True,reduced_motion='reduce')
        errors=[];page.on('pageerror',lambda exc:errors.append(str(exc)))
        page.goto(url);page.get_by_role('heading',name='我的微信档案').wait_for()
        assert page.locator('#demo-label').is_visible()
        # Exported readers omit an asset callback; avatars must use relative paths.
        assert page.evaluate("ChatRenderer.message({id:1,kind:'text',body:'fixture',sender_name:'friend',sender_avatar:'assets/avatar.png',detail:{} }).includes('src=\"assets/avatar.png\"')")

        page.locator('[data-chat="demo_xiaoyu"]').click()
        page.locator('#messages .kind-quote').wait_for()
        page.locator('#chat-compose-text').fill('这段只是待检查草稿')
        page.locator('[data-action="mirror-preflight"]').click()
        page.locator('#mirror-need').fill('希望先商量时间')
        assert page.locator('#mirror-preflight-result').is_visible()
        assert page.locator('#chat-compose-text').input_value() == '这段只是待检查草稿'
        page.locator('#messages').evaluate('(e)=>e.scrollTop=e.scrollHeight')
        page.screenshot(path=str(output/'shiguang-chat.png'))
        if page.locator('#messages .message-image').count():
            page.locator('#messages .message-image').first.evaluate("e=>e.src='/media/missing/image.png'")
            page.locator('#messages .missing-media').first.wait_for()
            assert page.locator('#messages [data-action="retry-media"]').count() > 0
        page.locator('[data-ref]').last.click()
        page.locator('.reference-highlight').wait_for()
        page.locator('#message-search').fill('完全没有的字符串')
        page.get_by_text('没有找到符合条件的消息',exact=True).wait_for()
        page.locator('[data-action="reset-messages"]').click()
        page.locator('#messages .kind-quote').wait_for()
        page.locator('#message-date').fill('2025-02-10');page.locator('[data-action="jump-date"]').click()
        page.wait_for_function('messages.length > 0 && messages.every(m=>m.time.slice(0,10)<="2025-02-10")')
        page.locator('#navigation [data-nav="contacts"]').click();page.locator('#contact-rows tr').first.wait_for()
        page.locator('#contact-search').fill('小予')
        page.wait_for_function('document.querySelectorAll("#contact-rows tr").length===1 && document.querySelector("#contact-rows").textContent.includes("小予")')
        page.screenshot(path=str(output/'shiguang-contacts.png'))
        page.locator('#navigation [data-nav="media"]').click();page.get_by_role('heading',name='媒体管理').wait_for()
        page.locator('#media-sync').wait_for();page.screenshot(path=str(output/'shiguang-media.png'))
        page.locator('#navigation [data-nav="automation"]').click();page.get_by_role('heading',name='自动回复').wait_for()
        page.locator('#listen-random-emoji').wait_for()
        assert page.locator('input[name="listen-mode"]').count()==2
        page.locator('input[name="listen-mode"][value="fixed"]').check()
        assert page.locator('#listen-fixed-fields').is_visible()
        assert not page.locator('#listen-ai-fields').is_visible()
        page.locator('input[name="listen-mode"][value="ai"]').check()
        assert page.locator('#listen-ai-fields').is_visible()
        assert not page.locator('#listen-fixed-fields').is_visible()
        assert page.locator('#listen-role option').count() >= 8
        page.locator('#listen-search').fill('小予')
        page.locator('[data-action="listen-select-all"]').click()
        assert page.locator('[data-listen-contact="demo_xiaoyu"]').is_checked()
        page.locator('#listen-search').fill('')
        page.locator('#navigation [data-nav="proactive"]').click();page.get_by_role('heading',name='主动消息').wait_for()
        page.locator('#proactive-search').fill('no-such-contact-999')
        assert page.locator('[data-proactive-row]:visible').count()==0
        page.locator('#proactive-search').fill('')
        assert page.locator('[data-proactive-row]:visible').count()>0
        page.locator('#proactive-search').fill('小予')
        page.locator('[data-action="proactive-select-all"]').click()
        assert page.locator('[data-proactive-contact="demo_xiaoyu"]').is_checked()
        page.locator('#proactive-search').fill('')
        page.screenshot(path=str(output/'shiguang-proactive.png'))
        page.locator('#navigation [data-nav="insights"]').click();page.get_by_role('heading',name='关系镜像',exact=True).wait_for()
        assert page.locator('#mirror-start').is_visible() and page.locator('#mirror-end').is_visible()
        page.locator('input[name="insight-contact"]').first.check()
        page.wait_for_function("document.querySelector('#mirror-start')?.min.length===10 && document.querySelector('#mirror-end')?.min.length===10")
        page.wait_for_function("document.querySelector('#mirror-start')?.min === document.querySelector('#mirror-end')?.min")
        page.wait_for_function("document.querySelector('#mirror-start')?.closest('.choice-row')?.nextElementSibling?.textContent.includes('该好友有')")
        assert page.locator('[data-action="mirror-all"]').is_visible()
        page.locator('#insight-search').fill('no-such-contact-999')
        assert page.locator('[data-insight-row]:visible').count()==0
        page.locator('#insight-search').fill('')
        assert page.locator('[data-insight-row]:visible').count()>0
        page.locator('input[name="mirror-source"][value="upload"]').check()
        page.locator('#mirror-upload-text').fill('A：你好\nB：你好\nA：明天有空吗\nB：下午有空')
        assert page.locator('input[name="mirror-self"]').count()==0
        page.locator('[data-action="mirror-preview"]').click()
        page.locator('#mirror-preview-box .mirror-preview-lines p').first.wait_for()
        assert page.locator('#mirror-preview-box .mirror-preview-lines p').count()==4
        page.locator('input[name="mirror-self"][value="A"]').check()
        page.locator('input[name="mirror-source"][value="local"]').check()
        page.route('**/api/insights/skill/start', lambda request: request.fulfill(
            status=200, content_type='application/json', body=json.dumps(
                {'id': 'demo-style-task', 'status': 'running', 'percent': 0, 'stage': '正在读取聊天记录'})))
        skill_polls = {'count': 0}
        def skill_status(request):
            skill_polls['count'] += 1
            done = skill_polls['count'] >= 4
            request.fulfill(status=200, content_type='application/json', body=json.dumps({
                'id': 'demo-style-task', 'status': 'done' if done else 'running',
                'percent': 100 if done else 42,
                'stage': '技能已生成并保存' if done else '观察语言习惯 2/4',
                'result': {'kind': 'my_style', 'content': 'fixture style'}}))
        page.route('**/api/insights/skill/demo-style-task', skill_status)
        page.locator('[data-action="insight-style"]').click()
        page.locator('.insight-skill-progress').get_by_text('观察语言习惯 2/4', exact=False).wait_for()
        page.wait_for_function("document.querySelector('.insight-skill-progress progress')?.value === 42")
        page.wait_for_function("document.querySelector('.insight-skill-progress progress')?.value === 100")
        page.unroute('**/api/insights/skill/start')
        page.unroute('**/api/insights/skill/demo-style-task')
        page.screenshot(path=str(output/'shiguang-insights.png'))
        page.locator('#navigation [data-nav="model"]').click();page.get_by_role('heading',name='AI 模型').wait_for()
        page.locator('#listen-api-url').wait_for()
        assert page.locator('[data-action="test-model"]').is_visible()
        address = page.locator('#listen-api-url').bounding_box()
        test_button = page.locator('[data-action="test-model"]').bounding_box()
        assert address and test_button and test_button['x'] >= address['x'] + address['width']
        assert abs(test_button['y'] - address['y']) < 12
        page.locator('[data-action="test-model"]').scroll_into_view_if_needed()
        page.screenshot(path=str(output/'shiguang-model-connection.png'))
        page.locator('#listen-provider').select_option('openai')
        assert page.locator('#listen-api-url').input_value() == 'https://api.openai.com/v1'
        page.locator('#available-models').evaluate("node=>{node.innerHTML='<option value=\"model-a\">model-a</option><option value=\"model-b\">model-b</option>';node.hidden=false;}")
        page.locator('#available-models').select_option('model-b')
        assert page.locator('#listen-model').input_value() == 'model-b'
        def fake_model_save(route):
            if route.request.method == 'POST':
                route.fulfill(status=200, content_type='application/json', body='{}')
            else:
                route.continue_()
        page.route('**/api/automation', fake_model_save)
        page.locator('[data-action="save-model"]').click()
        assert page.locator('#listen-model').is_visible()
        page.unroute('**/api/automation', fake_model_save)
        page.screenshot(path=str(output/'shiguang-model.png'))
        page.locator('#navigation [data-nav="export"]').click();page.locator('#export-all').wait_for()
        page.locator('#export-all').uncheck();page.locator('[data-export-chat="demo_xiaoyu"]').check()
        for fmt in ['docx','txt','csv','sqlite','contacts']:page.locator('[data-format="'+fmt+'"]').check()
        page.screenshot(path=str(output/'shiguang-export.png'))
        old_job=page.evaluate('state.jobs.at(-1)?.id || ""')
        page.locator('[data-action="start-export"]').click()
        page.wait_for_function('(old)=>state.jobs.at(-1)?.id!==old && state.jobs.at(-1)?.kind==="export" && state.jobs.at(-1)?.status==="done"',arg=old_job,timeout=60000)
        export=page.evaluate('state.jobs.filter(j=>j.kind==="export" && j.status==="done").at(-1).result')
        assert Path(export['path']).is_dir()
        page.locator('#modal .modal-top [data-action="close-modal"]').click()
        page.locator('#navigation [data-nav="report"]').click();page.locator('#report-year').select_option('2025')
        old_job=page.evaluate('state.jobs.at(-1)?.id || ""')
        page.locator('[data-action="generate-report"]').click()
        page.wait_for_function('(old)=>state.jobs.at(-1)?.id!==old && state.jobs.at(-1)?.kind==="report" && state.jobs.at(-1)?.status==="done"',arg=old_job,timeout=60000)
        report=page.evaluate('state.jobs.filter(j=>j.kind==="report" && j.status==="done").at(-1)')
        page.locator('#modal .modal-top [data-action="close-modal"]').click()
        page.locator('#report-output .stat-grid').wait_for();page.screenshot(path=str(output/'shiguang-report.png'))
        page.locator('[data-action="share-report"]').click();page.locator('.qr-content img').wait_for()
        share_url=page.locator('.qr-content input').input_value()
        assert page.request.get(share_url).status==200
        page.locator('#modal .modal-top [data-action="close-modal"]').click()
        standalone=browser.new_page(viewport={'width':1200,'height':900},bypass_csp=True,reduced_motion='reduce')
        html=next(f for f in Path(export['path']).glob('*.html') if f.name!='打开归档.html')
        standalone.goto(html.as_uri());standalone.locator('#messages .message-row').first.wait_for()
        standalone.locator('#search').fill('周末')
        standalone.wait_for_function('document.getElementById("status").textContent.includes("条消息")')
        standalone.locator('#date').fill('2026-09-25');standalone.locator('#jump').click()
        standalone.locator('[data-ref]').last.click();standalone.locator('.reference-highlight').wait_for()
        standalone.goto(Path(report['result']['index']).as_uri());standalone.locator('#words span').first.wait_for()
        standalone.screenshot(path=str(output/'shiguang-annual-full.png'),full_page=True)
        standalone.set_viewport_size({'width':390,'height':844});standalone.screenshot(path=str(output/'shiguang-annual-mobile.png'),full_page=True)
        assert standalone.evaluate('document.documentElement.scrollWidth <= 390')
        page.locator('#settings-nav').click();page.locator('#settings-output').wait_for();page.locator('[data-action="save-settings"]').click()
        page.get_by_text('设置已保存',exact=True).wait_for()
        page.set_viewport_size({'width':1100,'height':740});page.locator('#navigation [data-nav="home"]').click()
        page.get_by_role('heading',name='我的微信档案').wait_for();page.screenshot(path=str(output/'shiguang-home-compact.png'))
        assert page.evaluate('document.documentElement.scrollWidth <= 1100')
        assert not errors, errors
        print(json.dumps({'result':'passed','screenshots':len(list(output.glob('shiguang-*.png'))),'javascript_errors':errors,'exports':export['files'],'messages':export['messages']},ensure_ascii=True))
        browser.close()


if __name__=='__main__':main()
