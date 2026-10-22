// Architecture tests seeded by Agent Squad HQ (pack dotnet).
//
// Tool:  NetArchTest.Rules 1.3.2 in an xUnit test project:
//          dotnet add <YourApp.Tests.csproj> package NetArchTest.Rules --version 1.3.2
// Run:   dotnet test --filter "Category=Architecture"
//        (the hub's "arch" gate runs exactly this whenever a file named
//        *ArchitectureTests.cs exists)
//
// TODO(owner): replace "MyApp" with the root namespace, point AppAssemblies at
// one type from each production assembly, and record the layering in an ADR.
using System.Linq;
using System.Reflection;
using NetArchTest.Rules;
using Xunit;

namespace MyApp.Tests.Architecture;

[Trait("Category", "Architecture")]
public class ArchitectureTests
{
    private const string Root = "MyApp";

    // One type per assembly under test, e.g. typeof(MyApp.Domain.Order).Assembly.
    private static readonly Assembly[] AppAssemblies =
    {
        typeof(ArchitectureTests).Assembly, // TODO(owner): replace with the production assemblies
    };

    private static Types App() => Types.InAssemblies(AppAssemblies);

    private static string Failing(TestResult result) =>
        "Violations: " + string.Join(", ", (result.FailingTypeNames ?? Enumerable.Empty<string>()));

    [Fact]
    public void Domain_does_not_depend_on_infrastructure_or_the_web_layer()
    {
        var result = App()
            .That().ResideInNamespace($"{Root}.Domain")
            .ShouldNot().HaveDependencyOnAny(
                $"{Root}.Infrastructure", $"{Root}.Api", $"{Root}.Web",
                "Microsoft.AspNetCore", "Microsoft.EntityFrameworkCore")
            .GetResult();
        Assert.True(result.IsSuccessful, Failing(result));
    }

    [Fact]
    public void Application_does_not_depend_on_the_web_layer()
    {
        var result = App()
            .That().ResideInNamespace($"{Root}.Application")
            .ShouldNot().HaveDependencyOnAny($"{Root}.Api", $"{Root}.Web", "Microsoft.AspNetCore.Mvc")
            .GetResult();
        Assert.True(result.IsSuccessful, Failing(result));
    }

    [Fact]
    public void Controllers_do_not_use_the_database_directly()
    {
        var result = App()
            .That().HaveNameEndingWith("Controller")
            .ShouldNot().HaveDependencyOn("Microsoft.EntityFrameworkCore")
            .GetResult();
        Assert.True(result.IsSuccessful, Failing(result));
    }
}
