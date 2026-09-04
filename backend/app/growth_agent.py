import os, json

def explain_growth_opportunity(opportunity):
    """Optional Gemini explanation. Numeric facts remain backend-calculated."""
    api_key=os.getenv('GOOGLE_API_KEY') or os.getenv('GEMINI_API_KEY')
    fallback={
        'headline':'A measurable cross-sell opportunity was found.',
        'explanation':opportunity['reason'],
        'proposal':f"Test a bundle of {opportunity['recommendation']['primary_product']} with {opportunity['recommendation']['secondary_product']} using a {opportunity['suggested_discount']}% bounded discount.",
        'guardrail':'Merchant approval is required. This upgrade does not launch a campaign automatically.'
    }
    if not api_key: return fallback
    try:
        from google import genai
        client=genai.Client(api_key=api_key)
        prompt=f"""You are a merchant growth copilot. Use ONLY these supplied facts; never invent metrics. Return valid JSON with headline, explanation, proposal, guardrail.\n{json.dumps(opportunity)}"""
        r=client.models.generate_content(model=os.getenv('GEMINI_MODEL','gemini-2.5-flash'),contents=prompt)
        text=(r.text or '').strip()
        if text.startswith('```'): text=text.split('```',2)[1].replace('json','',1).strip()
        return json.loads(text)
    except Exception:
        return fallback
