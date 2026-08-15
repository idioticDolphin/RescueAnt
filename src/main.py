import model.analyzer.category_service as category_service
category_service.categorize_website("""
<!DOCTYPE html>
    <html lang="en">
    <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device; initial-scale=1.0">
    <link rel="icon" type="image/x-icon" />
    </head>
    <body>
    <h1>This is a web page of the animal rescue station Tierliebe Marburg.</h1>
    <p>We take in dogs only.</p>
    <p>Reach us at Liebigstraße 4, or via <a href=mailto:tierliebe-marburg@aol.com>mail</a>.</p>
    </body>""")