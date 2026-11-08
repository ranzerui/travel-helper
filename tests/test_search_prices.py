from datetime import date
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from travel_planner import browser_review, intent, planner, pricing, report
from travel_planner.models import Spot
from workers import review_browser


SOURCE = {"aliases": ["景山公园"]}
DAY = date(2026, 11, 6)


class SearchPriceTest(unittest.TestCase):
    def test_summary_prices_accept_common_formats(self):
        for text in ("景山公园门票多少钱\n成人票:2元/人", "景山公园门票\n成人¥2；展览活动期¥10", "景山公园\n门票价格：2元"):
            with self.subTest(text=text):
                rule = pricing.matching_price(pricing.extract_search_prices(text, SOURCE), DAY)
                self.assertIsNotNone(rule)
                self.assertEqual(rule["amount"], 2)
                self.assertEqual(rule["evidence_status"], "search")

    def test_first_baidu_answer_extracts_free_admission_before_extra_activity_note(self):
        text = ("交子公园是免费开放的城市公园，入园不需要购买门票，也无需预约。\n"
                "费用：公园日常参观完全免费，直接前往即可。\n"
                "注意：园内若举办特定商业活动或演出，可能会有单独收费项目，但进入公园本身不收费。")
        rule = pricing.matching_price(pricing.extract_search_prices(text, {"aliases": ["交子公园"]}), DAY)
        self.assertEqual(rule["amount"], 0)
        self.assertIn("交子公园是免费开放", rule["quote"])

    def test_summary_separates_regular_adult_price_from_exhibitions_and_concessions(self):
        text = ("北京景山公园日常门票‌2 元/人‌，举办展览期间调整为‌10 元/人‌。\n"
                "‌日常票价‌：成人全价票 2 元，半价票 1 元。\n"
                "‌展览票价‌：牡丹文化节、盆景展等临时展览期间，全价票 10 元，半价票 5 元。")
        rules = pricing.extract_search_prices(text, SOURCE)
        self.assertEqual({rule["amount"] for rule in rules}, {2})
        self.assertEqual(pricing.matching_price(rules, DAY)["amount"], 2)

    def test_suggested_price_questions_are_not_price_statements(self):
        self.assertEqual(pricing.extract_search_prices("景山公园门票10元还是2元", SOURCE), [])

    def test_qualifier_and_year_are_preserved_across_comma(self):
        self.assertEqual(pricing.extract_search_prices("景山公园学生优惠，成人票10元", SOURCE), [])
        rules = pricing.extract_search_prices("2024年，景山公园门票2元", SOURCE)
        self.assertIsNone(pricing.matching_price(rules, DAY))

    def test_range_uses_upper_bound_and_preserves_range(self):
        rule = pricing.extract_search_prices("景山公园门票2—10元", SOURCE)[0]
        self.assertEqual((rule["amount_min"], rule["amount_max"], rule["amount"]), (2, 10, 10))

    def test_seasons_require_matching_dates(self):
        rules = pricing.extract_search_prices("景山公园门票\n11月1日至次年3月31日成人票2元；4月1日至10月31日成人票5元", SOURCE)
        self.assertEqual(pricing.matching_price(rules, DAY)["amount"], 2)
        self.assertEqual(pricing.matching_price(rules, date(2027, 6, 1))["amount"], 5)

    def test_ambiguous_or_discount_and_old_prices_are_not_adopted(self):
        for text in ("景山公园学生票1元", "景山公园成人票2元起", "景山公园成人票¥2起", "景山公园旺季门票5元", "景山公园游船门票30元"):
            with self.subTest(text=text):
                self.assertEqual(pricing.extract_search_prices(text, SOURCE), [])
        rules = pricing.extract_search_prices("2024年景山公园门票2元", SOURCE)
        self.assertIsNone(pricing.matching_price(rules, DAY))

    def test_free_summary_requires_correct_subject(self):
        self.assertEqual(pricing.extract_search_prices("故宫门票免费", SOURCE), [])
        self.assertEqual(pricing.extract_search_prices("天安门广场门票免费", {"aliases": ["天安门"]}, "天安门广场门票"), [])
        self.assertEqual(pricing.extract_search_prices("景山公园门票免费", SOURCE)[0]["amount"], 0)

    def test_official_price_has_priority_over_search_summary(self):
        rules = pricing.extract_search_prices("景山公园门票2元", SOURCE)
        rules.append({"amount": 3, "quote": "成人票3元", "ticket_type": "成人大门票"})
        spot = Spot("park", "景山公园", "北京", "自然", 39.93, 116.4, "09:00", "18:00", 90, ticket_prices=tuple(rules))
        self.assertEqual(pricing.ticket_details(spot, DAY)["status"], "official")
        self.assertEqual(pricing.ticket_details(spot, DAY)["amount"], 3)

    def test_search_only_record_updates_cost_without_claiming_official_verification(self):
        spot = Spot("park", "景山公园", "北京", "自然", 39.93, 116.4, "09:00", "18:00", 90)
        itinerary, result = planner.plan("北京", [spot], 1, 200, start_date=DAY)
        rules = [{**rule, "engine": "百度", "url": "https://www.baidu.com/s?wd=test"}
                 for rule in pricing.extract_search_prices("景山公园门票2元", SOURCE)]
        review = {"checked": [{"spot_id": spot.id, "name": spot.name, "status": "failed", "pages": [], "ticket_search": {"prices": rules}}],
                  "initial": browser_review._snapshot(itinerary, [spot]), "changes": [], "unchecked": []}
        req = intent.Requirements(city="北京", days=1, budget=200)
        final, checked = browser_review.apply_review(req, [spot], itinerary, result, review)
        self.assertEqual(final.budget, 200)
        self.assertEqual(checked.ticket_total, 2)
        self.assertEqual(checked.results[0].legs[0]["ticket_status"], "search")
        self.assertFalse(checked.budget_complete)
        self.assertFalse(spot.verified)
        self.assertIn("搜索参考", report.to_markdown(final, checked))


class SearchWorkerTest(unittest.IsolatedAsyncioTestCase):
    async def test_capture_types_full_query_and_presses_enter_before_snapshot(self):
        page = AsyncMock()
        field = AsyncMock()
        hidden = AsyncMock()
        page.get_elements_by_css_selector.return_value = [hidden, field]
        session = review_browser.asyncio.Future()
        session.set_result("owned-page-session")
        page.session_id = session
        client = AsyncMock()
        page.get_url.return_value = "https://www.baidu.com/s?wd=test"
        page.get_title.return_value = "搜索结果"
        page.screenshot.return_value = "iVBORw0KGgo="
        query = "景山公园门票多少钱"
        page.evaluate.side_effect = ["1", json.dumps({"input": query, "encoding": "UTF-8", "url": "https://www.baidu.com/"})] + ["景山公园门票" * 30] * 4 + ["[]"]
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(review_browser.discovery, "public_url", return_value=True), \
                patch.object(review_browser, "wait_search_answer", new_callable=AsyncMock, return_value={"status": "ready"}), \
                patch.object(review_browser.asyncio, "sleep", new_callable=AsyncMock):
            captured, _ = await review_browser.capture_page(page, page.get_url.return_value,
                {"domains": ["www.baidu.com"]}, Path(temporary), "search", analyze=False,
                submit={"query": query, "input": "#chat-textarea,#kw", "parameter": "wd", "blocks": "#content_left > div"}, cdp_client=client)
        field.fill.assert_not_awaited()
        hidden.focus.assert_not_awaited()
        field.focus.assert_awaited_once()
        client.send.Input.insertText.assert_awaited_once_with({"text": query}, session_id="owned-page-session")
        self.assertEqual([call.args[0] for call in page.press.await_args_list], ["Control+a", "Enter"])
        self.assertEqual(captured["submission"]["input_query"], query)
        self.assertEqual(captured["submission"]["input_method"], "Input.insertText")
        self.assertEqual(captured["submission"]["encoding"], "UTF-8")

    async def test_waits_through_first_card_loading_and_streaming(self):
        page = AsyncMock()
        values = ["正在思考", "正在搜索\n半亩塘艺术村门票多少钱", "半亩塘艺术村门票", "半亩塘艺术村门票免费"]
        page.evaluate.side_effect = [json.dumps({"blocks": [{"text": value}]}) for value in values + [values[-1]] * 2]
        with patch.object(review_browser.asyncio, "sleep", new_callable=AsyncMock):
            result = await review_browser.wait_search_answer(page, "#content_left > div")
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["polls"], 6)
        self.assertEqual(result["first_card"], "半亩塘艺术村门票免费")

    async def test_loading_timeout_is_reported_as_pending_not_missing_price(self):
        page = AsyncMock()
        page.evaluate.return_value = json.dumps({"blocks": [{"text": "正在思考"}]})
        with patch.object(review_browser.asyncio, "sleep", new_callable=AsyncMock):
            result = await review_browser.wait_search_answer(page, "#content_left > div", max_polls=4)
        self.assertEqual(result["status"], "answer_pending")

    async def test_partial_unicode_input_is_never_submitted(self):
        page = AsyncMock()
        page.get_elements_by_css_selector.return_value = [AsyncMock()]
        session = review_browser.asyncio.Future()
        session.set_result("owned-page-session")
        page.session_id = session
        page.evaluate.side_effect = ["0", json.dumps({"input": "交", "encoding": "UTF-8", "url": "https://www.baidu.com/"})]
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(review_browser.discovery, "public_url", return_value=True), \
                patch.object(review_browser.asyncio, "sleep", new_callable=AsyncMock):
            with self.assertRaisesRegex(ValueError, "未收到完整文字"):
                await review_browser.capture_page(page, "https://www.baidu.com/", {"domains": ["www.baidu.com"]},
                    Path(temporary), "search", analyze=False, submit={"query": "交子公园门票多少钱", "input": "#kw", "parameter": "wd"}, cdp_client=AsyncMock())
        self.assertNotIn("Enter", [call.args[0] for call in page.press.await_args_list])

    async def test_baidu_result_card_is_read_before_fallback(self):
        page = AsyncMock()
        query = "景山公园门票多少钱"
        page.evaluate.side_effect = [json.dumps({"input": query, "blocks": [{"title": "景山公园门票价格", "text": "景山公园成人票2元", "url": "https://example.com/park", "ad": False}]}), True, True]
        page.screenshot.return_value = "iVBORw0KGgo="
        captured = {"url": "https://www.baidu.com/s?wd=" + review_browser.quote(query), "captured_at": "2026-11-06", "screenshot": "entry.png", "submission": {"method": "Enter"}}
        with tempfile.TemporaryDirectory() as temporary, patch.object(review_browser, "capture_page", new_callable=AsyncMock, return_value=(captured, [])) as capture:
            result = await review_browser.search_ticket_price(page, {"city": "北京", "spot_name": "景山公园", **SOURCE}, Path(temporary), 0, [DAY.isoformat()])
        self.assertEqual(capture.await_count, 1)
        self.assertEqual(result["prices"][0]["engine"], "百度")
        self.assertEqual(result["prices"][0]["amount"], 2)
        self.assertEqual(result["status"], "reference")
        self.assertTrue(result["attempts"][0]["submitted"])
        self.assertEqual(result["attempts"][0]["matched_result_count"], 1)

    async def test_captcha_is_recorded_then_bing_is_used(self):
        page = AsyncMock()
        page.get_url.return_value = "https://wappass.baidu.com/static/captcha/test"
        page.get_title.return_value = "百度安全验证"
        page.screenshot.return_value = "iVBORw0KGgo="
        query = "景山公园门票多少钱"
        page.evaluate.side_effect = [json.dumps({"input": query, "blocks": [{"title": "景山公园票价", "text": "景山公园成人票2元", "url": "https://example.com/park", "ad": False}]}), True, True]
        captured = {"url": "https://cn.bing.com/search?q=" + review_browser.quote(query), "captured_at": "2026-11-06", "screenshot": "entry.png", "submission": {"method": "Enter"}}
        with tempfile.TemporaryDirectory() as temporary, patch.object(review_browser, "capture_page", new_callable=AsyncMock, side_effect=[ValueError("captcha"), (captured, [])]):
            result = await review_browser.search_ticket_price(page, {"city": "北京", "spot_name": "景山公园", **SOURCE}, Path(temporary), 0, [DAY.isoformat()])
        self.assertIn("安全验证", result["attempts"][0]["error"])
        self.assertTrue(result["attempts"][0]["blocked_page"]["screenshot"])
        self.assertEqual(result["prices"][0]["engine"], "必应")

    async def test_unrelated_bing_results_retry_then_fall_back_without_prices(self):
        page = AsyncMock()
        query = "交子公园门票多少钱"
        quoted = '"交子公园" 门票'
        dictionary = [{"title": "交_百度百科", "text": "交，汉字。门票200元", "url": "https://example.com/dict"}]
        related = [{"title": "交子公园门票", "text": "交子公园门票免费", "url": "https://example.com/park"}]
        page.evaluate.side_effect = [json.dumps({"input": query, "blocks": dictionary}),
            json.dumps({"input": query, "blocks": dictionary}), json.dumps({"input": quoted, "blocks": dictionary}),
            json.dumps({"input": query, "blocks": related}), True, True]
        page.screenshot.return_value = "iVBORw0KGgo="
        captures = [({"url": review_browser.SEARCH_ENGINES[engine]["base"] + review_browser.quote(term),
            "captured_at": "2026-11-06", "screenshot": "entry.png", "submission": {"method": "Enter"}}, [])
            for engine, term in (("百度", query), ("必应", query), ("必应", quoted), ("360", query))]
        with tempfile.TemporaryDirectory() as temporary, patch.object(review_browser, "capture_page", new_callable=AsyncMock, side_effect=captures):
            result = await review_browser.search_ticket_price(page, {"spot_name": "交子公园", "aliases": ["交子公园"], "city": "成都"}, Path(temporary), 0, [DAY.isoformat()])
        self.assertEqual([attempt["status"] for attempt in result["attempts"]], ["result_mismatch"] * 3 + ["matched"])
        self.assertEqual(result["prices"][0]["engine"], "360")
        self.assertEqual(result["prices"][0]["amount"], 0)
        self.assertTrue(all(not attempt.get("prices") for attempt in result["attempts"][:3]))


class SearchValidationTest(unittest.TestCase):
    def test_full_query_in_box_cannot_authenticate_dictionary_results(self):
        query = "交子公园门票"
        result, blocks = review_browser.validate_search(query, query, query,
            [{"title": "交_百度百科", "text": "交 jiāo - 汉典"}], ["交子公园"])
        self.assertEqual(result["status"], "result_mismatch")
        self.assertEqual(blocks, [])

    def test_url_and_input_must_preserve_entire_submitted_query(self):
        for box, url in (("交", "交子公园门票"), ("交子公园门票", "交")):
            result, _ = review_browser.validate_search("交子公园门票", box, url,
                [{"title": "交子公园门票", "text": "门票免费"}], ["交子公园"])
            self.assertEqual(result["status"], "query_mismatch")

    def test_ads_do_not_establish_result_relevance(self):
        result, blocks = review_browser.validate_search("交子公园门票", "交子公园门票", "交子公园门票",
            [{"title": "交子公园门票", "text": "门票200元", "ad": True}], ["交子公园"])
        self.assertEqual(result["status"], "result_mismatch")
        self.assertEqual(blocks, [])

    def test_related_queries_are_not_search_answer_cards(self):
        query = "景山公园门票多少钱"
        result, blocks = review_browser.validate_search(query, query, query,
            [{"title": "", "text": "大家还在搜\n景山公园门票10元还是2元"}], ["景山公园"])
        self.assertEqual(result["status"], "result_mismatch")
        self.assertEqual(blocks, [])


if __name__ == "__main__":
    unittest.main()
