import re
html = open('index.html').read()
if 'formatQuoteBalance' in html:
    print("formatQuoteBalance is present")
