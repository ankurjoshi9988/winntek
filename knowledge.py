from flask import Blueprint, render_template, request, jsonify
from PyPDF2 import PdfReader
from langchain.text_splitter import RecursiveCharacterTextSplitter
import os
from langchain_google_genai import GoogleGenerativeAIEmbeddings
import google.generativeai as genai
from langchain_community.vectorstores import FAISS
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain.chains.question_answering import load_qa_chain
from langchain.prompts import PromptTemplate
from dotenv import load_dotenv
from flask_login import login_required
import camelot
from pytesseract import image_to_string
import pdf2image
import re

knowledge_bp = Blueprint("recall", __name__, url_prefix="/recall")


def configure_google_api() -> None:
    """Load environment variables and configure Google API."""
    load_dotenv()
    api_key = os.getenv("GOOGLE_API_KEY")
    if api_key is None:
        raise ValueError("GOOGLE_API_KEY not found in environment variables.")
    genai.configure(api_key=api_key)


configure_google_api()


def extract_text_from_images(pdf_path: str) -> str:
    """Extract text from images in the given PDF file using OCR."""
    images = pdf2image.convert_from_path(pdf_path)
    ocr_text = ""
    for i, img in enumerate(images):
        print(f"Processing image {i + 1}...")
        img = img.convert('L')  # Convert to grayscale
        text = image_to_string(img)
        ocr_text += text + "\n"
        print(f"OCR Text from image {i + 1}:\n{text}\n")
    return ocr_text


def extract_tables_from_pdf(pdf_path: str) -> str:
    """Extract tables from the given PDF file."""
    try:
        tables = camelot.read_pdf(pdf_path, pages='all', flavor='stream', strip_text='\n')
        table_text = ""

        for i, table in enumerate(tables):
            print(f"Extracting table {i + 1} of {len(tables)}...")
            table_data = table.df.to_string(index=False)
            table_text += f"\n--- Table {i + 1} ---\n" + table_data + "\n"
            print(f"Extracted Table {i + 1}:\n{table_data}\n")

        print(f"Total {len(tables)} tables extracted.")
        return table_text
    except Exception as e:
        print(f"Error extracting tables: {e}")
        return ""


def get_pdf_text(pdf_docs: list) -> str:
    """Extract text, tables, and images from the given PDF files."""
    text = ""
    for pdf in pdf_docs:
        try:
            pdf_reader = PdfReader(pdf)
            for page_num, page in enumerate(pdf_reader.pages, start=1):
                page_text = page.extract_text()
                text += page_text
                print(f"Extracted text from page {page_num}:\n{page_text}\n")

            # Extract tables and images
            table_text = extract_tables_from_pdf(pdf)
            image_text = extract_text_from_images(pdf)

            # Combine all extracted content
            text += "\n" + table_text + "\n" + image_text

        except Exception as e:
            print(f"Error reading PDF file: {e}")
    return text


def get_text_chunks(text: str) -> list:
    """Split the given text into chunks for further processing."""
    text_splitter = RecursiveCharacterTextSplitter(chunk_size=10000, chunk_overlap=1000)
    return text_splitter.split_text(text)


def get_vector_store(text_chunks: list) -> None:
    """Create and save a vector store from the text chunks."""
    embeddings = GoogleGenerativeAIEmbeddings(model="models/embedding-001")
    vector_store = FAISS.from_texts(text_chunks, embedding=embeddings)
    vector_store.save_local("faiss_index")


def get_conversational_chain():
    """Create and return a conversational QA chain."""
    prompt_template = """
    You are proficient in all language. Answer the question(s) in a language user asks. It should be detailed and well-structured manner based on the provided context.
    Provide a clear, structured response with each detail on a separate line, and ensure that there is appropriate spacing between the different sections.
    Start by providing a brief introduction if necessary, and then format the response with each section (e.g., "Policy Term:", "Maturity Age:", "Premium Payment Term:", "Income Start Year:") on a new line.
    If the answer is not found in the provided context, simply state, "उत्तर संदर्भ में उपलब्ध नहीं है" (answer is not available in the context).
    Avoid generating "**" symbols or special characters in response.
    Do not provide any incorrect information.
    
    संदर्भ (Context):\n {context}\n
    प्रश्न (Question):\n{question}\n
    
    उत्तर (Answer):
    """

    model = ChatGoogleGenerativeAI(model="gemini-pro", temperature=0.3)
    prompt = PromptTemplate(template=prompt_template, input_variables=["context", "question"])
    return load_qa_chain(model, chain_type="stuff", prompt=prompt)


@knowledge_bp.route("/", methods=["GET"])
@login_required
def render_knowledge_page():
    """Render the knowledge page."""
    return render_template("recall.html")


@knowledge_bp.route("/upload", methods=["POST"])
@login_required
def upload_document():
    """Handle the document upload and extract information."""
    pdf_docs = request.files.getlist("pdf_docs")
    if pdf_docs:
        print(pdf_docs)
        raw_text = get_pdf_text(pdf_docs)
        text_chunks = get_text_chunks(raw_text)
        get_vector_store(text_chunks)
        message = "Document uploaded..."
        return jsonify(message=message)
    else:
        return jsonify(message="No PDF files uploaded.")


@knowledge_bp.route("/ask", methods=["POST"])
def ask_question():
    """Handle user questions and provide answers."""
    try:
        data = request.get_json()
        user_question = data.get("question", "")
        if user_question:
            response = user_input(user_question)
            return jsonify(response=response["output_text"])
        else:
            return jsonify(response="No question provided"), 400
    except Exception as e:
        print(f"Error handling request: {e}")
        return jsonify(response="Internal Server Error"), 500


def format_response(response: str) -> str:
    """Format the response text for better readability."""
    formatted = re.sub(r"(\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*:\s*)", r"\n\1", response)
    formatted = re.sub(r"(\.\s+)([A-Z])", r"\1\n\2", formatted)
    formatted = re.sub(r"(\s+)(Yes|No),", r"\n\n\2,", formatted)
    return formatted


def user_input(user_question: str) -> dict:
    """Process user input to generate a response."""
    embeddings = GoogleGenerativeAIEmbeddings(model="models/embedding-001")
    new_db = FAISS.load_local("faiss_index", embeddings, allow_dangerous_deserialization=True)

    docs = new_db.similarity_search(user_question)
    context_parts = [doc.page_content.replace("\n", " ").strip() for doc in docs]
    context = " ".join(context_parts)

    if not context:
        return {"output_text": "The relevant information could not be found in the provided context."}

    chain = get_conversational_chain()

    try:
        response = chain.invoke({"input_documents": docs, "question": user_question})
        formatted_response = format_response(response["output_text"])
    except Exception as e:
        print(f"Error during chain invocation: {e}")
        formatted_response = "An error occurred while processing your request. Please try again."

    return {"output_text": formatted_response}
