import asyncio
import io
import httpx

async def main():
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8000", timeout=30.0) as client:
        # 1. Login
        res = await client.post("/auth/login", json={"email": "caregiver@demo.id", "password": "demo1234"})
        token = res.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        res = await client.get("/me", headers=headers)
        budi_id = next(e["id"] for e in res.json()["elders"] if "Budi" in e["name"])
        print(f"Testing Phase 8 Documents on Pak Budi ({budi_id})...")

        # 2. Upload document
        fake_pdf_content = b"%PDF-1.4 Fake PDF Content for medical test..."
        files = {
            "file": ("hasil_rontgen_dada.pdf", io.BytesIO(fake_pdf_content), "application/pdf")
        }
        data = {
            "title": "Hasil Rontgen Dada"
        }
        res = await client.post(
            f"/elders/{budi_id}/documents",
            headers=headers,
            data=data,
            files=files
        )
        print("POST /documents status:", res.status_code, res.json())
        assert res.status_code == 201
        doc_data = res.json()
        doc_id = doc_data["id"]
        file_url = doc_data["file_url"]
        assert doc_data["title"] == "Hasil Rontgen Dada"
        assert doc_data["mime"] == "application/pdf"
        assert doc_data["size_bytes"] == len(fake_pdf_content)

        # 3. List documents
        res = await client.get(f"/elders/{budi_id}/documents", headers=headers)
        print("GET /documents count:", len(res.json()))
        assert res.status_code == 200
        assert any(d["id"] == doc_id for d in res.json())

        # 4. Acceptance check: file_url opens
        print(f"Opening file_url: {file_url}...")
        res_file = await client.get(file_url)
        print("GET file_url status:", res_file.status_code)
        assert res_file.status_code == 200
        assert res_file.content == fake_pdf_content
        print("File content matched successfully!")

        # 5. Delete document
        res = await client.delete(f"/elders/{budi_id}/documents/{doc_id}", headers=headers)
        print("DELETE document status:", res.status_code)
        assert res.status_code == 200

        # Verify no longer in list
        res = await client.get(f"/elders/{budi_id}/documents", headers=headers)
        assert not any(d["id"] == doc_id for d in res.json())
        print("Document deletion verified!")

        print("\n=== PHASE 8 ACCEPTANCE CHECK PASSED! ===")

if __name__ == "__main__":
    asyncio.run(main())
