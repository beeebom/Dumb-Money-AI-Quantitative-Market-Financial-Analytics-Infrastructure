import os
import requests
import re
from googlesearch import search  # pip install googlesearch-python

def download_company_pdf(ticker):
    """
    Finds the official recent quarterly report or financial results PDF 
    using public search indexing and saves it into your company folder.
    """
    print(f"\nScanning the web for recent official PDF reports for: {ticker}...")
    
    # 1. We construct a highly specific search string to find indexable public PDFs
    search_query = f"{ticker} quarterly financial results filetype:pdf site:screener.in OR site:bseindia.com OR investor relations"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    
    # Create the company folder structure
    company_folder = f"pdf_reports/{ticker}"
    os.makedirs(company_folder, exist_ok=True)
    
    try:
        # 2. Run the search and grab the top 3 matching web links
        links = list(search(search_query, num_results=3))
        
        if not links:
            print(f"No open public report links found for {ticker}.")
            return
            
        download_count = 0
        for i, url in enumerate(links):
            # Only try to download if the link actually points to an online file or report page
            if not url.startswith("http"):
                continue
                
            print(f" -> Found matching report target: {url}")
            print("    Attempting direct download stream...")
            
            try:
                # 3. Stream the raw binary data down from the web address
                pdf_response = requests.get(url, headers=headers, timeout=15)
                
                if pdf_response.status_code == 200:
                    # Establish a clean, alphanumeric file name
                    clean_url_snippet = re.sub(r'[^a-zA-Z0-9]', '_', url[-25:])
                    pdf_filename = f"{company_folder}/{ticker}_report_{i+1}_{clean_url_snippet}.pdf"
                    
                    # Write the binary data directly into the file
                    with open(pdf_filename, 'wb') as f:
                        f.write(pdf_response.content)
                        
                    print(f"    SUCCESS! Saved to: {pdf_filename}")
                    download_count += 1
                    
                    # Stop once we have successfully captured 1 or 2 solid documents
                    if download_count >= 2:
                        break
                else:
                    print(f"    Skipping: Server returned status code {pdf_response.status_code}")
                    
            except Exception as download_error:
                print(f"    Could not download this specific link, trying the next one...")
                continue
                
    except Exception as search_error:
        print(f"Error running search pipeline for {ticker}: {search_error}")

# --- EXECUTE DOWNLOAD ENGINE FOR YOUR TARGET ENTITIES ---
if __name__ == "__main__":
    # You don't need codes or numbers anymore, just pass the plain company names!
    companies_to_download = ["TATAMOTORS"]
    
    for company in companies_to_download:
        download_company_pdf(company)