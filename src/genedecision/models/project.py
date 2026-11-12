"""Research-project schema."""

from pydantic import BaseModel, ConfigDict, Field


class Project(BaseModel):
    """A researcher-defined candidate-gene project."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    project_name: str = Field(min_length=1)
    organism: str = Field(min_length=1)
    disease: str = Field(min_length=1)
    tissue: str = Field(min_length=1)
    phenotype: str = Field(min_length=1)
    drug_or_treatment: str = Field(min_length=1)
    research_question: str = Field(min_length=1)
    candidate_genes: list[str] = Field(min_length=1)
