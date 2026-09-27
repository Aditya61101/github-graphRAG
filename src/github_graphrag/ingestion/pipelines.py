from neo4j_graphrag.experimental.pipeline.kg_builder import SimpleKGPipeline

def build_pipeline(
    llm,
    driver,
    embedder,
    text_splitter,
    database: str,
) -> SimpleKGPipeline:
    return SimpleKGPipeline(
        llm=llm,
        driver=driver,
        embedder=embedder,
        from_file=False,
        text_splitter=text_splitter,
        schema=None,  # automatic schema extraction
        perform_entity_resolution=True,
        neo4j_database=database,
        on_error="RAISE",
    )