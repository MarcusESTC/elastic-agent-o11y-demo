"""Generate the historical September 24 guide, not the current ingestion design.

See docs/PII-Redaction-Elastic.md for the current native OTel pipeline.
"""
from pathlib import Path
from reportlab.pdfgen import canvas
from reportlab.lib.colors import HexColor, white
from reportlab.platypus import Paragraph
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_LEFT
from pypdf import PdfReader
import fitz

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'output/pdf/Elastic-PII-Redaction-One-Pager.pdf'
OUT.parent.mkdir(parents=True,exist_ok=True)
c=canvas.Canvas(str(OUT),pagesize=(612,792))
c.setTitle('Configure PII redaction in Elastic')
c.setAuthor('Elastic Observability Demo')
navy=HexColor('#10242E');teal=HexColor('#007F78');muted=HexColor('#4C626C');line=HexColor('#D4E0E3')

def para(text,x,y,w=540,size=9.2,color=navy,bold=False):
    style=ParagraphStyle('p',fontName='Helvetica-Bold' if bold else 'Helvetica',fontSize=size,leading=size*1.4,textColor=color,spaceAfter=0)
    p=Paragraph(text,style);_,h=p.wrap(w,800);p.drawOn(c,x,y-h);return y-h

def label(n,text,x,y):
    c.setFillColor(teal);c.roundRect(x,y-17,19,19,5,fill=1,stroke=0)
    c.setFillColor(white);c.setFont('Helvetica-Bold',9);c.drawCentredString(x+9.5,y-11,n)
    para(text,x+28,y,300,11,bold=True);return y-27

def code(text,x,y,w=324,size=7.4):
    rows=text.splitlines();leading=10.2;h=len(rows)*leading+20
    c.setFillColor(HexColor('#F0F5F6'));c.roundRect(x,y-h,w,h,6,fill=1,stroke=0)
    c.setFillColor(navy);c.setFont('Courier',size)
    for i,row in enumerate(rows):
        assert c.stringWidth(row,'Courier',size)<w-20,(row,'too wide')
        c.drawString(x+10,y-15-i*leading,row)
    return y-h-10

c.setFillColor(navy);c.rect(0,682,612,110,fill=1,stroke=0)
c.setFillColor(HexColor('#5BE0C3'));c.setFont('Helvetica-Bold',9);c.drawString(36,761,'ELASTIC  /  IMPLEMENTATION QUICK GUIDE')
c.setFillColor(white);c.setFont('Helvetica-Bold',24);c.drawString(36,726,'Configure PII redaction')
c.setFillColor(HexColor('#CDE0E5'));c.setFont('Helvetica',10);c.drawString(36,704,'Keep the transaction. Obfuscate the stored telemetry.')
y=para('A PII match replaces sensitive text; it does not fail the application request. '+
       'Use this example for ordinary Elasticsearch ingest-pipeline documents.',36,667,size=10)
c.setStrokeColor(line);c.line(36,622,576,622)
left,right=36,382
y=label('1','Create the pipeline',left,608)
y=para('In Kibana <b>Dev Tools</b>, run:',left,y,324,size=9)
y=code('''PUT _ingest/pipeline/pii-redact-demo
{"processors":[{"redact":{
  "field":"message",
  "patterns":["%{CARD:CREDIT_CARD}",
              "%{EMAILADDRESS:EMAIL}"],
  "pattern_definitions":{"CARD":
  "(?<![0-9])(?:[0-9][ -]?){12,18}[0-9](?![0-9])"},
  "prefix":"[", "suffix":"]",
  "skip_if_unlicensed":false
}}]}''',left,y-7)
y=para('Requires an appropriate <b>redact license</b> and setup permissions. The card pattern is a heuristic for 13-19 digits, with optional spaces or hyphens.',left,y,324,size=8.5)
y=label('2','Test without storing data',left,y-18)
y=code('''POST _ingest/pipeline/pii-redact-demo/_simulate
{"docs":[{"_source":{
  "message":"Card 4111 1111 1111 1111; demo@example.com"
}}]}''',left,y,324,size=7.2)
y=para('<b>Expected:</b> Card [CREDIT_CARD]; [EMAIL]',left,y,324,size=9,color=teal)
y=label('3','Attach to a new demo index',left,y-20)
y=code('''PUT pii-demo-logs
{"settings":{
  "index.final_pipeline":"pii-redact-demo"
}}''',left,y)
y=para('Index documents into <b>pii-demo-logs</b>. For production, integrate with the appropriate index template and preserve existing processing.',left,y,324,size=8.5)

ry=608
for title,body in [
 ('Successful request','PII matches are transformed normally. Keep telemetry delivery separate from application success. If export fails, handle it independently; never send the original text through an unprotected fallback.'),
 ('OTel-native path','Elastic currently documents that ingest pipelines do not apply to OTel-native data. Use EDOT/OTel Collector processing or source masking for that path. Cover bodies, attributes and trace events.'),
 ('Verify the result','Read the indexed <b>_source</b>: placeholders present, original values absent. Confirm the application trace is successful. Add rules for every sensitive field; remove fields you do not need.'),
 ('Scope matters','Pattern matching does not detect all PII. Existing documents are not automatically cleaned. Redaction in Elastic cannot protect content already sent to an LLM.')]:
    ry=para(title,right,ry,194,size=10,color=teal,bold=True)-7
    ry=para(body,right,ry,194,size=9)-21

assert y>113 and ry>100,(y,ry)
c.setFillColor(HexColor('#E6F4EF'));c.roundRect(36,65,540,42,6,fill=1,stroke=0)
para('<b>DEMO VERIFIED</b>  PII request completed with APM status <b>OK</b>; Elasticsearch stored a masked log. '+
     'The native OTel copies and model input also use source masking.',47,97,518,size=8.4,color=teal)
c.setStrokeColor(line);c.line(36,52,576,52)
c.setFillColor(muted);c.setFont('Helvetica',7);c.drawString(36,39,'Official documentation:')
links=[('Redact processor','https://www.elastic.co/docs/reference/ingest-processor/redact-processor'),
       ('Simulate API','https://www.elastic.co/docs/api/doc/elasticsearch/operation/operation-ingest-simulate'),
       ('Final pipeline','https://www.elastic.co/docs/reference/elasticsearch/index-settings/index-modules'),
       ('OTel processing','https://www.elastic.co/docs/reference/edot-collector/config/configure-logs-collection')]
x=115
for title,url in links:
    c.setFillColor(teal);c.drawString(x,39,title);width=c.stringWidth(title,'Helvetica',7)
    c.linkURL(url,(x,37,x+width,46),relative=0);x+=width+13
c.setFillColor(muted);c.drawString(36,23,'September 24, 2026  |  Uses public test data. No credentials included.')
c.save()
assert len(PdfReader(str(OUT)).pages)==1
doc=fitz.open(str(OUT));page=doc[0]
preview=ROOT/'tmp/pdfs/pii-onepager.png';preview.parent.mkdir(parents=True,exist_ok=True)
page.get_pixmap(matrix=fitz.Matrix(2,2)).save(str(preview))
print(OUT)
print('Validated: 1 page; preview:',preview)
