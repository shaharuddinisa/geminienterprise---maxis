# geminienterprise---maxis
Maxis Berhad is a leading integrated telecommunications provider in Malaysia, offering a comprehensive range of mobile, fixed-line, and enterprise digital services. Headquartered in Kuala Lumpur, the company serves millions of individual and business customers while investing heavily in 5G infrastructure and digital transformation solutions.

Business Challenges
•
Intense price competition and market saturation in the Malaysian mobile sector
•
High capital expenditure requirements for 5G network rollout and infrastructure modernization
•
Increasing demand for seamless digital customer experiences and personalized service
•
Managing cybersecurity threats and data privacy compliance in a highly connected environment

# Maxis Berhad (Telecommunications)

## Target Role
AI Operations Architect & Customer Experience Automation Lead

## Business Scenario
Maxis Berhad, as a premier telecommunications provider in Malaysia, is currently navigating a high-stakes digital transformation phase. The company manages a massive subscriber base across mobile and enterprise segments, generating millions of data points daily across CRM (Salesforce), billing systems (Oracle BRM), and enterprise lead management platforms. Despite high investment in 5G, the company faces significant operational friction: customer support teams are overwhelmed by high-volume, low-complexity billing queries, while enterprise sales teams struggle to prioritize leads amidst a sea of unverified prospect data. To maintain market leadership, Maxis requires an autonomous AI agent capable of bridging the gap between analytical data sources and operational execution. The agent will act as a real-time orchestrator, reconciling billing discrepancies, automating subscription adjustments, and qualifying enterprise leads to ensure that human resources are reserved for high-value strategic interactions, thereby optimizing First Contact Resolution (FCR) and lead conversion velocity.

## Operational Challenge
The AI agent is tasked with executing a multi-stream autonomous workflow that integrates analytical data with real-time operational database updates.

1. **Trigger & Data Ingestion**:
   - **Billing Reconciliation**: The agent monitors daily billing logs exported as CSV files from the legacy billing system. It must parse these files to identify discrepancies (e.g., overcharges exceeding RM 50.00 or unauthorized service activations).
   - **Visual/HITL Trigger**: For enterprise contract renewals, the agent must process scanned PDF/JPEG images of signed physical service agreements. It uses multimodal vision to extract key terms (contract duration, service tier, SLA commitments) and reconciles these against the CRM data. If the extracted data deviates from the digital record, the agent must flag the entry in Firestore for Human-in-the-Loop (HITL) manager approval.

2. **Business Rules & Thresholds**:
   - **Automated Resolution**: If a billing discrepancy is verified as a system error under RM 200.00, the agent is authorized to trigger an automatic credit adjustment in the billing platform and notify the customer via SMS/Email.
   - **Lead Qualification**: The agent analyzes enterprise prospect engagement logs. Leads with a "Lead Score" > 75 (based on whitepaper downloads, webinar attendance, and pricing page visits) are automatically routed to the sales CRM with a prioritized status. Leads with a score < 40 are tagged for automated nurturing campaigns.

3. **Database Integration & Output**:
   - **Firestore Write-back**: All actions, including successful adjustments and flagged exceptions, must be written to the Firestore operational database to ensure the real-time console reflects the current state of all accounts.
   - **Executive Reporting & UI**: Upon completion of the daily batch, the agent generates a summary report in JSON format saved to BigQuery for trend analysis. Simultaneously, it pushes an interactive A2UI card to the Sales Manager’s dashboard, displaying a summary of "High-Value Leads Qualified Today" and "Pending HITL Approvals," allowing for one-click review of the flagged visual contract anomalies.
