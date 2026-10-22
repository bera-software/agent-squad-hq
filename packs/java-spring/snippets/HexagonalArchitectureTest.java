// Place in src/test/java/<your/root/package>/architecture/ (or backend/... in a multi-module repo)
// Adjust the root package (com.example) to yours. Requires ArchUnit (see build snippets).
package com.example.architecture;

import com.tngtech.archunit.junit.AnalyzeClasses;
import com.tngtech.archunit.junit.ArchTest;
import com.tngtech.archunit.lang.ArchRule;
import static com.tngtech.archunit.lang.syntax.ArchRuleDefinition.noClasses;
import static com.tngtech.archunit.library.Architectures.onionArchitecture;

@AnalyzeClasses(packages = "com.example")
class HexagonalArchitectureTest {

    @ArchTest
    static final ArchRule domain_is_framework_free = noClasses()
        .that().resideInAPackage("..domain..")
        .should().dependOnClassesThat().resideInAnyPackage(
            "..adapter..", "org.springframework..", "jakarta.persistence..",
            "jakarta.servlet..", "com.fasterxml.jackson..");

    @ArchTest
    static final ArchRule hexagon = onionArchitecture()
        .domainModels("..domain.model..")
        .domainServices("..domain.service..")
        .applicationServices("..application..")
        .adapter("persistence", "..adapter.persistence..")
        .adapter("web", "..adapter.web..")
        .adapter("external", "..adapter.external..")
        .withOptionalLayers(true);
}
