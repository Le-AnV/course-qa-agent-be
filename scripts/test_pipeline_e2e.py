import asyncio
import json
import sys

import httpx

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

BASE_URL = "http://127.0.0.1:8000"

async def test_scenarios():
    async with httpx.AsyncClient(timeout=120.0) as client:
        # 1. Login
        login_res = await client.post(
            f"{BASE_URL}/api/auth/login",
            json={"email": "admin@academic.edu.vn", "password": "Admin@123"},
            headers={"Content-Type": "application/json"}
        )
        if login_res.status_code != 200:
            print(f"Login failed: {login_res.status_code} {login_res.text}")
            return
        token = login_res.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        print("Logged in successfully.")

        # 2. Get classes
        classes_res = await client.get(f"{BASE_URL}/api/admin/classes", headers=headers)
        if classes_res.status_code != 200:
            print(f"Failed to get classes: {classes_res.status_code} {classes_res.text}")
            return
        classes = classes_res.json()
        if not classes:
            print("No classes found!")
            return
        
        # Choose C++ class or first class
        selected_class = None
        for c in classes:
            name = (c.get("name") or c.get("title") or c.get("code") or "").lower()
            if "c++" in name or "cpp" in name:
                selected_class = c
                break
        if not selected_class:
            selected_class = classes[0]
        class_id = selected_class["id"]
        class_title = selected_class.get("name") or selected_class.get("title") or selected_class.get("code")
        print(f"Selected Class: ID={class_id}, Name='{class_title}'")

        # Define 3 scenarios
        scenarios = [
            {
                "id": 1,
                "name": "Kịch bản 1: Code Help (Con trỏ C++)",
                "question": "int a = 10;\nint* ptr = &a;\n*ptr = 20;\nstd::cout << a;\nĐoạn code trên in ra gì và giải thích cơ chế con trỏ hoạt động?"
            },
            {
                "id": 2,
                "name": "Kịch bản 2: Khái niệm C++ (C++ có phân biệt chữ hoa chữ thường không?)",
                "question": "C++ có phân biệt chữ hoa chữ thường không?"
            },
            {
                "id": 3,
                "name": "Kịch bản 3: Khái niệm hàm đệ quy trong C++",
                "question": "Giải thích khái niệm hàm đệ quy trong C++ và cho ví dụ minh họa"
            }
        ]

        results_summary = []

        for sc in scenarios:
            print("\n" + "=" * 60)
            print(f"BẮT ĐẦU {sc['name']}")
            print("=" * 60)

            # Send chat stream request
            events = []
            citations = []
            suggestions = []
            answer_tokens = []
            action = None

            async with client.stream(
                "POST",
                f"{BASE_URL}/api/chat",
                headers=headers,
                json={"class_id": class_id, "message": sc["question"]}
            ) as response:
                current_event = None
                async for line in response.aiter_lines():
                    line = line.strip()
                    if not line:
                        continue
                    if line.startswith("event: "):
                        current_event = line[len("event: "):].strip()
                    elif line.startswith("data: "):
                        data_str = line[len("data: "):].strip()
                        events.append((current_event, data_str))
                        if current_event in ("token", "text"):
                            try:
                                payload = json.loads(data_str)
                                answer_tokens.append(payload.get("text", ""))
                            except (AttributeError, TypeError, ValueError):
                                answer_tokens.append(data_str)
                        elif current_event == "citation":
                            try:
                                payload = json.loads(data_str)
                                citations.append(payload)
                            except (AttributeError, TypeError, ValueError):
                                citations.append(data_str)
                        elif current_event == "suggestions":
                            try:
                                payload = json.loads(data_str)
                                suggestions.extend(payload.get("questions", []))
                            except (AttributeError, TypeError, ValueError):
                                suggestions.append(data_str)
                        elif current_event == "done":
                            print("[Done Event Received]")
                            try:
                                payload = json.loads(data_str)
                                action = payload.get("action")
                            except (AttributeError, TypeError, ValueError):
                                action = None

            full_answer = "".join(answer_tokens).strip()
            print(f"Action: {action}")
            print(f"Answer Preview: {full_answer[:200]}...")
            print(f"Full Answer Length: {len(full_answer)} chars")
            print(f"Citations Count: {len(citations)}")
            for i, cite in enumerate(citations):
                source_name = cite.get("document_name") or cite.get("url") or cite.get("title")
                page = cite.get("page_label") or cite.get("page_number")
                print(f"  [{i+1}] {source_name} (Page: {page})")
            print(f"Suggestions Count: {len(suggestions)}")
            for i, sug in enumerate(suggestions):
                print(f"  (?) {sug}")

            # Verify conditions
            has_error = False
            error_reasons = []
            if len(full_answer) == 0:
                has_error = True
                error_reasons.append("Câu trả lời bị rỗng!")
            if len(citations) == 0:
                has_error = True
                error_reasons.append("Không có Citation!")
            if len(suggestions) == 0:
                has_error = True
                error_reasons.append("Không có Follow-up suggestions!")
            if "I could not find enough supporting information" in full_answer or "The answer could not be generated" in full_answer:
                has_error = True
                error_reasons.append("Bị rơi vào safe fallback/lỗi!")

            passed = not has_error
            status_text = "PASS" if passed else f"FAIL ({', '.join(error_reasons)})"
            results_summary.append({
                "scenario": sc["name"],
                "action": action,
                "answer_length": len(full_answer),
                "citations_count": len(citations),
                "suggestions_count": len(suggestions),
                "status": status_text
            })
            print(f"-> Kết quả: {status_text}")

        print("\n" + "=" * 60)
        print("TỔNG HỢP KẾT QUẢ KIỂM THỬ 3 KỊCH BẢN")
        print("=" * 60)
        for r in results_summary:
            print(f"- {r['scenario']}: Status={r['status']} | Action={r['action']} | Citations={r['citations_count']} | Suggestions={r['suggestions_count']}")

if __name__ == "__main__":
    asyncio.run(test_scenarios())
