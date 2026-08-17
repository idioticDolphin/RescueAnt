import model.analyzer.category_service as category_service
import model.analyzer.extraction_service as extraction_service
import model.crawler.fetching_service as fetching_service
from pathlib import Path
import asyncio

root_path = Path(__file__).parent.parent
fetching_service.read_starting_urls(root_path / "starting_urls_test.csv")
asyncio.run(fetching_service.parse_queue())
fetched = fetching_service.processed_urls
for url in fetched.keys():
    html = fetched[url]
    category = category_service.categorize_website(html)
    print(f"{url} has category: {category.name}")
    extracted = extraction_service.extract_information(html, category, url)
    print(f"Extracted information for {url}:\n{extracted}")