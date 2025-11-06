import os
import pandas as pd
import numpy as np
from typing import Dict, Any, Optional

try:
    import openai
except Exception:
    openai = None

class MakePaymentAgent:
    """
    Prototype Make Payment agent.
    - Loads borrower/payment data from an Excel file (sheet 'borrowers').
    - Exposes process_request(input) which follows the flowchart logic from the PDF.
    - Optionally calls Azure OpenAI if environment variables are configured; otherwise uses a mock response.
    """

    def __init__(self, excel_path: str):
        self.excel_path = excel_path
        # Changed to use first sheet (index 0) instead of named sheet
        self.df = pd.read_excel(self.excel_path, sheet_name=0)
        self.df.columns = [c.strip() for c in self.df.columns]

    def _convert_to_native_types(self, obj):
        """Convert numpy/pandas types to native Python types for JSON serialization"""
        if isinstance(obj, dict):
            return {key: self._convert_to_native_types(value) for key, value in obj.items()}
        elif isinstance(obj, list):
            return [self._convert_to_native_types(item) for item in obj]
        elif isinstance(obj, (np.integer, np.int64)):
            return int(obj)
        elif isinstance(obj, (np.floating, np.float64)):
            return float(obj)
        elif isinstance(obj, np.ndarray):
            return obj.tolist()
        elif pd.isna(obj):
            return None
        else:
            return obj

    def _get_row(self, borrower_id: Any) -> Optional[pd.Series]:
        rows = self.df[self.df['BorrowerId'] == borrower_id]
        if len(rows) == 0:
            return None
        return rows.iloc[0]

    def _call_llm(self, prompt: str) -> str:
        endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
        key = os.getenv("AZURE_OPENAI_KEY")
        deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT")
        api_version = os.getenv("AZURE_OPENAI_API_VERSION", "2024-02-15-preview")

        if endpoint and key and deployment and openai is not None:
            try:
                # Modern OpenAI library (v1.0+)
                from openai import AzureOpenAI
                
                client = AzureOpenAI(
                    azure_endpoint=endpoint,
                    api_key=key,
                    api_version=api_version
                )
                
                response = client.chat.completions.create(
                    model=deployment,
                    messages=[
                        {
                            "role": "system", 
                            "content": """You are a mortgage payment virtual agent operating inside a voice AI system.
    You must follow THESE RULES EXACTLY:

    # OBJECTIVE
    - Help the borrower understand their due payment status and guide them to resolution.
    - Follow the flowchart logic and business rules strictly.
    - Respond in short, conversational sentences suitable for voice calls.

    # COMPLIANCE & RESTRICTIONS
    - Do NOT deviate from the data provided.
    - Never invent account data, dates, or fees.
    - If information is missing, ask for it.
    - Never provide legal, tax, personal financial planning advice.
    - Never promise outcomes the system cannot guarantee.
    - Do not store personal data beyond returned values.

    # IF DAYS LATE > 15
    - Ask empathetically for the reason they fell behind.
    - If user refuses, politely re-ask ONCE then continue.

    # FEES
    - If FeesBalance > 0, disclose them plainly.

    # ACCOUNT ON FILE
    - If no account is on file:
        * Offer to collect routing number + last 4 + account type.
    - If account exists:
        * Ask if borrower wants to use account on file.

    # PAYMENT CHOICES
    Borrower may:
    - Pay regular total due
    - Pay total due + fees
    - Schedule payment
    - Promise to pay later

    If TotalAmountDue > TotalPaymentDue:
    - Offer scheduling option.

    # ALREADY PAID
    If PaymentStatus = "already paid":
    - Acknowledge payment and disposition as 'Promise to Pay'.

    # ESCALATION
    If borrower asks questions outside scope:
    - Say: "I can only assist with payment questions. Would you like to continue with a payment today?"

    # WRAP-UP
    - Always provide a confirmation number after a successful payment.
    - Summarize payment details concisely.

    # TONE
    - Professional
    - Empathetic
    - Calm
    - Avoid jargon

    # PROHIBITED
    - Don't apologize repeatedly
    - Don't pressure payment
    - Don't mention system internals
    - Don't say you're an AI model
    - Don't disclose routing/account number completely"""
                        },
                        {
                            "role": "user", 
                            "content": prompt
                        }
                    ],
                    max_tokens=300,
                    temperature=0.2
                )
                
                return response.choices[0].message.content.strip()
                
            except Exception as e:
                return f"(LLM call failed: {e})"

        # Mock response when Azure OpenAI is not configured
        return "I understand that sometimes unexpected circumstances can make it difficult to stay current with payments. Could you share what led to this situation? This information helps us work together to find the best path forward for you."

    def process_request(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        trace = []
        borrower_id = payload.get("borrower_id")
        if borrower_id is None:
            return {"error": "borrower_id required", "trace": trace}

        row = self._get_row(borrower_id)
        if row is None:
            return {"error": f"No borrower with id {borrower_id}", "trace": trace}

        trace.append({"step": "start", "note": f"Loaded borrower {borrower_id}"})

        next_due = str(row.get("NextPaymentDueDate", ""))
        total_payment_due = float(row.get("TotalPaymentDue", 0)) if pd.notna(row.get("TotalPaymentDue")) else 0.0
        trace.append({"step": "display_due", "message": f"Due for {next_due}: {total_payment_due}"})

        fees_balance = float(row.get("FeesBalance", 0)) if pd.notna(row.get("FeesBalance")) else 0.0
        if fees_balance and fees_balance > 0:
            msg = f"I also see that you have additional fees in the amount of {fees_balance}."
            trace.append({"step": "fees", "FeesBalance": fees_balance, "agent_msg": msg})
        else:
            trace.append({"step": "fees", "FeesBalance": fees_balance, "agent_msg": "No fees"})

        account_on_file = bool(pd.notna(row.get("AccountType")) and str(row.get("AccountType")).strip() != "")
        account_type = str(row.get("AccountType")) if pd.notna(row.get("AccountType")) else None
        trace.append({"step": "account_on_file", "AccountType": account_type})

        restrict_autopay = str(row.get("restrict_autopay_draft", "")).strip().upper()
        trace.append({"step": "restrict_autopay", "value": restrict_autopay})

        days_late = int(row.get("Days Late", 0)) if pd.notna(row.get("Days Late")) else 0
        trace.append({"step": "days_late", "value": days_late})
        if days_late > 15:
            q = "What made you fall behind in making your payment?"
            trace.append({"step": "delinquency_question", "agent_msg": q})

            suggested = self._call_llm("Provide a concise empathetic prompt asking for reason for missed payment.")
            trace.append({"step": "llm_suggested_reason_prompt", "llm": suggested})

        payment_status = str(row.get("PaymentStatus", "")).strip().lower()
        trace.append({"step": "payment_status", "value": payment_status})
        if payment_status == "already paid":
            trace.append({"step": "already_paid", "action": "Make Payment - Promise to Pay (disposition)"})
            result = {"result": "already_paid", "trace": trace}
            return self._convert_to_native_types(result)

        decision = payload.get("decision", "pay_now")  # pay_now / schedule / promise_to_pay / no
        trace.append({"step": "decision_received", "decision": decision})

        if not account_on_file:
            trace.append({"step": "no_account_on_file", "agent_msg": "I see we don't have an account on file. Would you like a one-time payment today?"})
            if decision == "pay_now":
                trace.append({"step": "collect_account_info", "required": True})
                acct = {
                    "ach_account_type": payload.get("ach_account_type", "checking"),
                    "routing_number": payload.get("RoutingNumber", "111000025"),
                    "last_four": payload.get("Last Four of Account", "1234")
                }
                trace.append({"step": "account_collected", "account": acct})
            else:
                trace.append({"step": "no_account_action", "note": "Borrower chose not to provide account info."})
        else:
            trace.append({"step": "account_available", "note": "Using account on file"})

        total_amount_due = float(row.get("TotalAmountDue", 0)) if pd.notna(row.get("TotalAmountDue")) else 0.0
        total_payment_due = float(row.get("TotalPaymentDue", 0)) if pd.notna(row.get("TotalPaymentDue")) else 0.0
        trace.append({"step": "amounts", "TotalAmountDue": total_amount_due, "TotalPaymentDue": total_payment_due})

        if total_amount_due > total_payment_due:
            trace.append({"step": "amounts_compare", "note": "TotalAmountDue > TotalPaymentDue"})
            if decision == "schedule" or decision == "pay_now_and_schedule":
                trace.append({"step": "schedule_future", "agent_msg": "I can schedule a future payment for you. When would you like it?"})
            elif decision == "pay_now":
                trace.append({"step": "pay_now_partial", "note": "Borrower chooses to pay now"})
        else:
            trace.append({"step": "amounts_compare", "note": "TotalAmountDue <= TotalPaymentDue"})

        if decision in ("pay_now", "pay_now_and_schedule"):
            trace.append({"step": "process_payment", "status": "success", "method": "ACH" if account_on_file else "One-time ACH"})
            trace.append({"step": "wrap_up", "confirmation_number": "CONF-123456"})
            result = {"result": "payment_processed", "trace": trace, "confirmation": "CONF-123456"}
            return self._convert_to_native_types(result)

        if decision == "promise_to_pay":
            trace.append({"step": "promise_to_pay", "status": "noted"})
            trace.append({"step": "wrap_up", "confirmation_number": "PROMISE-123"})
            result = {"result": "promise_to_pay", "trace": trace, "confirmation": "PROMISE-123"}
            return self._convert_to_native_types(result)

        trace.append({"step": "no_action", "note": "No payment action taken."})
        result = {"result": "no_action", "trace": trace}
        return self._convert_to_native_types(result)