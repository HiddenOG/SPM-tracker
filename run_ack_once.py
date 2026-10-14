import sys
sys.path.insert(0, r"c:\Users\Godson\spm-tracker\scripts")
from dotenv import load_dotenv
load_dotenv(r"c:\Users\Godson\spm-tracker\.env")
import ack_pdf_extractor
ack_pdf_extractor.run_pass()
