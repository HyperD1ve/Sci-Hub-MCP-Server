import os
import re
import urllib3
import requests
from bs4 import BeautifulSoup

# 禁用 HTTPS 证书验证警告
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Working mirrors as of 2026-08. The old `scihub` pip package pinned dead hosts
# (sci-hub.tw / sci-hub.is), so mirrors are handled here directly. Override with
# a comma-separated SCIHUB_MIRRORS environment variable if these rotate again.
DEFAULT_MIRRORS = [
    "https://sci-hub.ru",
    "https://sci-hub.st",
    "https://sci-hub.se",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

TIMEOUT = 30


def get_mirrors():
    """返回镜像列表，可通过 SCIHUB_MIRRORS 环境变量覆盖"""
    env = os.environ.get("SCIHUB_MIRRORS", "").strip()
    if env:
        return [m.strip().rstrip("/") for m in env.split(",") if m.strip()]
    return DEFAULT_MIRRORS


def _absolutize(url, mirror):
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return mirror + url
    return url


def _parse_article_page(html, mirror):
    """从 Sci-Hub 文章页面提取 PDF 链接和元数据

    Current mirrors expose the PDF via a <meta name="citation_pdf_url"> tag;
    older page layouts used an <embed>/<iframe> or an onclick download button,
    kept here as fallbacks.
    """
    soup = BeautifulSoup(html, "html.parser")

    pdf_url = None
    meta_pdf = soup.find("meta", attrs={"name": "citation_pdf_url"})
    if meta_pdf and meta_pdf.get("content"):
        pdf_url = meta_pdf["content"]
    if not pdf_url:
        emb = soup.find("embed") or soup.find("iframe")
        if emb and emb.get("src"):
            pdf_url = emb["src"]
    if not pdf_url:
        btn = soup.find("button", onclick=True)
        if btn:
            match = re.search(r"location\.href='([^']+)'", btn["onclick"])
            if match:
                pdf_url = match.group(1)
    if pdf_url:
        pdf_url = _absolutize(pdf_url.split("#")[0], mirror)

    def _meta(name):
        tag = soup.find("meta", attrs={"name": name})
        return tag["content"].strip() if tag and tag.get("content") else ""

    authors = [t["content"].strip() for t in soup.find_all("meta", attrs={"name": "citation_author"}) if t.get("content")]

    metadata = {
        "title": _meta("citation_title"),
        "author": "; ".join(authors),
        "year": _meta("citation_publication_date"),
    }
    return pdf_url, metadata


def search_paper_by_doi(doi, attempts=2):
    """通过 DOI 在 Sci-Hub 上搜索论文

    Mirrors occasionally serve a transient page without the PDF link, so the
    mirror list is tried up to `attempts` times before giving up.
    """
    last_error = None
    for mirror in get_mirrors() * max(1, attempts):
        try:
            resp = requests.get(f"{mirror}/{doi}", headers=HEADERS, timeout=TIMEOUT, verify=False)
            if resp.status_code != 200 or len(resp.text) < 200:
                last_error = f"{mirror} returned HTTP {resp.status_code}"
                continue
            pdf_url, metadata = _parse_article_page(resp.text, mirror)
            if not pdf_url:
                last_error = f"{mirror} has no PDF for this DOI"
                continue
            return {
                "doi": doi,
                "pdf_url": pdf_url,
                "status": "success",
                "title": metadata["title"],
                "author": metadata["author"],
                "year": metadata["year"],
            }
        except requests.RequestException as e:
            last_error = f"{mirror}: {e}"
            continue
    print(f"搜索出错: {last_error}")
    return {
        "doi": doi,
        "status": "not_found",
        "error": last_error or "no mirror produced a PDF link",
    }


def search_paper_by_title(title):
    """通过标题在 Sci-Hub 上搜索论文（先经 CrossRef 找 DOI）"""
    try:
        url = f"https://api.crossref.org/works?query.title={title}&rows=1"
        response = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        if response.status_code == 200:
            data = response.json()
            if data["message"]["items"]:
                doi = data["message"]["items"][0]["DOI"]
                return search_paper_by_doi(doi)
    except Exception as e:
        print(f"CrossRef 搜索出错: {str(e)}")

    return {
        "title": title,
        "status": "not_found",
    }


def search_papers_by_keyword(keyword, num_results=10):
    """通过关键词搜索论文，返回元数据列表"""
    papers = []
    try:
        url = f"https://api.crossref.org/works?query={keyword}&rows={num_results}"
        response = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        if response.status_code == 200:
            data = response.json()
            for item in data["message"]["items"]:
                doi = item.get("DOI")
                if doi:
                    result = search_paper_by_doi(doi)
                    if result["status"] == "success":
                        papers.append(result)
    except Exception as e:
        print(f"搜索出错: {str(e)}")

    return papers


def download_paper(pdf_url, output_path):
    """下载论文 PDF（pdf_url 来自 search_paper_by_doi 的结果）"""
    try:
        resp = requests.get(pdf_url, headers=HEADERS, timeout=90, verify=False)
        if resp.status_code != 200:
            print(f"下载出错: HTTP {resp.status_code}")
            return False
        if resp.content[:4] != b"%PDF":
            print("下载出错: 返回内容不是 PDF（可能是验证码页面）")
            return False
        with open(output_path, "wb") as f:
            f.write(resp.content)
        return True
    except Exception as e:
        print(f"下载出错: {str(e)}")
        return False


if __name__ == "__main__":
    print("Sci-Hub 论文搜索测试\n")

    test_doi = "10.1190/1.1443518"  # Ikelle et al. 1993, Geophysics
    result = search_paper_by_doi(test_doi)
    print(result)

    if result["status"] == "success":
        output_file = f"paper_{test_doi.replace('/', '_')}.pdf"
        if download_paper(result["pdf_url"], output_file):
            print(f"论文已下载到: {output_file}")
        else:
            print("论文下载失败")
